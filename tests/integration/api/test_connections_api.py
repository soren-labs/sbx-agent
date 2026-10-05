"""One Connection model: lifecycle, CAS validation, revoke, isolation, durability (A18/A19)."""

from __future__ import annotations

import os

import pytest
from control.composition import build_services
from control.domain.errors import DomainError
from control.domain.ids import new_id
from control.jobs import claims
from control.security.vault import Vault, VaultError
from tests.support.api import ApiStack, User, fake_validators, make_config

ZEN = "zen-live-like-key-ABCDEFGHIJ0123"
MODAL = {"token_id": "ak-test0000000000001", "token_secret": "as-secret00000000000001"}
GH = "ghp_testtoken000000000000000000000000"


@pytest.fixture
def stack(db, tmp_path):
    s = ApiStack(db, tmp_path)
    yield s
    s.shutdown()


def test_manual_connections_validate_and_never_leak(stack) -> None:
    user = User(stack)
    zen = user.connect("opencode_zen", {"api_key": ZEN}, "My Zen")
    modal = user.connect("modal", MODAL)
    gh = user.connect("github", {"token": GH})
    assert zen["health"] == "verifying" and zen["credential"]["ordinal"] == 1
    stack.drain()
    listing = user.get(f"/api/workspaces/{user.workspace_id}/connections").json()["items"]
    by_kind = {c["kind"]: c for c in listing}
    assert {c["health"] for c in listing} == {"ready"}
    assert by_kind["github"]["external_identity"] == "octo-test"
    assert by_kind["opencode_zen"]["validation"]["quota_consuming"] is True
    models = user.get(f"/api/models?workspace_id={user.workspace_id}").json()
    assert models["preferred_model"] == "opencode/big-pickle"
    assert models["connections"][0]["models"][0]["free"] is True
    assert not any(c["kind"] == "codex" for c in listing), "no Codex required"
    text = user.all_text()
    for secret in (ZEN, MODAL["token_secret"], GH):
        assert secret not in text
    stored = stack.db.read(lambda u: u.find("credential_versions", {}))
    assert all(ZEN.encode() not in bytes(r["ciphertext"]) for r in stored)
    events_and_jobs = str(
        stack.db.read(
            lambda u: (
                u.find("jobs", {}),
                u.find("audit_records", {}),
                u.find("connection_observations", {}),
            )
        )
    )
    for secret in (ZEN, MODAL["token_secret"], GH):
        assert secret not in events_and_jobs
    del modal, gh


def test_replace_invalidates_and_stale_validation_cannot_overwrite(stack) -> None:
    """A19: validation of v1 finishing after replacement is discarded by CAS."""
    user = User(stack)
    zen = user.connect("opencode_zen", {"api_key": ZEN})
    claim = claims.claim_next(stack.db, "slow-worker", kinds=["connection.validate"])
    replaced = user.post(
        f"/api/connections/{zen['id']}/credential-versions",
        {
            "credential": {"api_key": "invalid-replacement-key-1"},
            "expected_version": zen["version"],
        },
    )
    assert replaced.status_code == 201 and replaced.json()["credential"]["ordinal"] == 2
    stale_version = user.post(
        f"/api/connections/{zen['id']}/credential-versions",
        {"credential": {"api_key": "zen-other-key-99999"}, "expected_version": zen["version"]},
    )
    assert stale_version.json()["error"]["code"] == "version_conflict"
    from control.jobs.worker import JobContext

    ctx = JobContext(stack.db, claim, stack.worker)
    assert stack.services.connections.handle_validate(ctx).result["skipped"] == "stale_or_revoked"
    stack.drain()
    current = user.get(f"/api/connections/{zen['id']}").json()
    assert current["health"] == "reauth_required" and current["credential"]["ordinal"] == 2
    con = stack.db.read(lambda u: u.get("connections", zen["id"]))
    assert con["revocation_epoch"] == 2


def test_validation_racing_replacement_commit_is_discarded(stack) -> None:
    user = User(stack)
    zen = user.connect("opencode_zen", {"api_key": ZEN})
    real = stack.services.connections.validators["opencode_zen"]

    def racing(material, **kw):
        stack.services.connections.validators["opencode_zen"] = real
        user.post(
            f"/api/connections/{zen['id']}/credential-versions",
            {"credential": {"api_key": "invalid-race-key-123"}, "expected_version": zen["version"]},
        )
        return real(material, **kw)

    stack.services.connections.validators["opencode_zen"] = racing
    stack.drain()
    current = user.get(f"/api/connections/{zen['id']}").json()
    assert current["credential"]["ordinal"] == 2 and current["health"] == "reauth_required"
    observations = stack.db.read(
        lambda u: u.find("connection_observations", {"connection_id": zen["id"]})
    )
    assert {o["credential_version_id"] for o in observations} == {current["credential"]["id"]}


def test_disconnect_rejected_while_compute_depends_then_revokes(stack) -> None:
    user = User(stack)
    modal = user.connect("modal", MODAL)
    stack.drain()
    sid = stack.db.read(lambda u: None)
    zen = user.connect("opencode_zen", {"api_key": ZEN})
    stack.drain()
    created = user.post(
        f"/api/workspaces/{user.workspace_id}/sessions",
        {"harness": {"provider_id": "opencode"}, "executor": {"backend": "modal"}},
    )
    assert created.status_code == 201, created.text
    sid = created.json()["session_id"]
    lease_id = new_id("lease")
    stack.db.run(
        lambda u: u.insert(
            "executor_leases",
            {
                "id": lease_id,
                "workspace_id": user.workspace_id,
                "session_id": sid,
                "backend": "modal",
                "allocation_operation_id": new_id("operation"),
                "generation": 1,
                "state": "ready",
                "compute_connection_id": modal["id"],
                "image_digest": "x",
                "resource_class": "standard",
            },
        )
    )
    refused = user.delete(f"/api/connections/{modal['id']}")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "connection_in_use"
    assert refused.json()["error"]["details"]["executor_leases"] == [lease_id]
    session = stack.db.read(lambda u: u.get("sessions", sid))
    assert stack.services.broker.compute(session)["token_id"] == MODAL["token_id"], (
        "teardown authority retained"
    )
    stack.db.run(lambda u: u.update("executor_leases", lease_id, {"state": "released"}))
    revoked = user.delete(f"/api/connections/{modal['id']}")
    assert (
        revoked.status_code == 200
        and revoked.json()["state"] == "revoked"
        and revoked.json()["credential"] is None
    )
    with pytest.raises(DomainError) as err:
        stack.services.broker.compute(session)
    assert err.value.code == "connection_revoked"
    again = user.post(
        f"/api/connections/{modal['id']}/credential-versions",
        {"credential": MODAL, "expected_version": revoked.json()["version"]},
    )
    assert again.json()["error"]["code"] == "connection_revoked"
    assert all(
        c["id"] != modal["id"]
        for c in user.get(f"/api/workspaces/{user.workspace_id}/connections").json()["items"]
    )
    assert zen["id"]


def test_no_ambient_credential_fallback(stack, monkeypatch) -> None:
    user = User(stack)
    for name, value in {
        "MODAL_TOKEN_ID": "ak-ambient",
        "MODAL_TOKEN_SECRET": "as-ambient",
        "GITHUB_TOKEN": GH,
        "OPENCODE_API_KEY": ZEN,
    }.items():
        monkeypatch.setenv(name, value)
    r = user.post(
        f"/api/workspaces/{user.workspace_id}/sessions",
        {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "connection_required"
    assert r.json()["error"]["details"]["kind"] == "opencode_zen"
    user.connect("opencode_zen", {"api_key": ZEN})
    stack.drain()
    r = user.post(
        f"/api/workspaces/{user.workspace_id}/sessions",
        {"harness": {"provider_id": "opencode"}, "executor": {"backend": "modal"}},
    )
    assert r.json()["error"]["details"]["kind"] == "modal"
    assert os.environ["MODAL_TOKEN_ID"] == "ak-ambient"


def test_cross_owner_isolation(stack) -> None:
    alice, bob = User(stack), User(stack)
    zen = alice.connect("opencode_zen", {"api_key": ZEN})
    stack.drain()
    created = alice.post(
        f"/api/workspaces/{alice.workspace_id}/sessions",
        {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}},
    ).json()
    sid = created["session_id"]
    for path in (
        f"/api/connections/{zen['id']}",
        f"/api/sessions/{sid}",
        f"/api/workspaces/{alice.workspace_id}/connections",
        f"/api/sessions/{sid}/events",
        f"/api/workspaces/{alice.workspace_id}/sessions",
    ):
        assert bob.get(path).status_code == 404, path
    steal = bob.post(
        f"/api/workspaces/{bob.workspace_id}/sessions",
        {
            "harness": {"provider_id": "opencode"},
            "executor": {"backend": "local"},
            "connections": {"inference": zen["id"]},
        },
    )
    assert steal.status_code == 404
    assert bob.delete(f"/api/connections/{zen['id']}").status_code == 404
    assert bob.post(f"/api/sessions/{sid}/messages", {"content": "hi"}).status_code == 404
    assert bob.get(f"/api/models?workspace_id={alice.workspace_id}").status_code == 404


def test_restart_reconstructs_connections_and_sessions(db, tmp_path) -> None:
    config = make_config(db, tmp_path)
    first = ApiStack(db, tmp_path, config=config)
    user = User(first)
    zen = user.connect("opencode_zen", {"api_key": ZEN})
    first.drain()
    sid = user.post(
        f"/api/workspaces/{user.workspace_id}/sessions",
        {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}},
    ).json()["session_id"]
    second = ApiStack(db, tmp_path, config=config)
    http = second.client()
    login = http.post("/api/auth/login", json={"email": user.email, "password": user.password})
    assert login.status_code == 200
    listed = http.get(f"/api/workspaces/{user.workspace_id}/connections").json()["items"]
    assert [c["id"] for c in listed] == [zen["id"]] and listed[0]["health"] == "ready"
    assert (
        http.get(f"/api/sessions/{sid}").json()["session"]["connections"]["inference"] == zen["id"]
    )
    session = db.read(lambda u: u.get("sessions", sid))
    bundle, meta = second.services.broker.inference(session)
    assert bundle["opencode_zen"]["api_key"] == ZEN and meta["connection_id"] == zen["id"]
    wrong = build_services(
        make_config(db, tmp_path), validators=fake_validators(), executors={}, db=db
    )
    with pytest.raises(VaultError):
        wrong.broker.inference(session)


def test_vault_binds_associated_data_and_rotates() -> None:
    old = Vault.from_spec(Vault.generate_spec("k1"))
    aad_a = Vault.aad("wsp_a", "con_a", "cred_a", "zen_api_key/v1")
    aad_b = Vault.aad("wsp_a", "con_b", "cred_a", "zen_api_key/v1")
    sealed = old.seal({"api_key": ZEN}, aad_a)
    with pytest.raises(VaultError):
        old.open(sealed, aad_b)
    rotated = Vault({"k2": os.urandom(32), "k1": old.keyring["k1"]}, "k2")
    assert rotated.open(sealed, aad_a)["api_key"] == ZEN
    assert rotated.seal({"x": "y"}, aad_a).key_id == "k2"
