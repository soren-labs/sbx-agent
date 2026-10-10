"""One Connection model: lifecycle, CAS validation, revoke, isolation, durability (A18/A19)."""

from __future__ import annotations

import os

import pytest
from control.composition import build_services
from control.domain.errors import DomainError
from control.domain.ids import new_id
from control.jobs import claims
from control.security.vault import Vault, VaultError
from tests.support.api import (
    INFERENCE_URL,
    ApiStack,
    User,
    fake_validators,
    inference,
    make_config,
)

ZEN = "inference-live-like-key-ABCDEFGHIJ0123"
MODAL = {"token_id": "ak-test0000000000001", "token_secret": "as-secret00000000000001"}
GH = "ghp_testtoken000000000000000000000000"


@pytest.fixture
def stack(db, tmp_path):
    s = ApiStack(db, tmp_path)
    yield s
    s.shutdown()


def test_manual_connections_validate_and_never_leak(stack) -> None:
    user = User(stack)
    zen = user.connect("inference_api", inference(ZEN), "My Zen")
    modal = user.connect("modal", MODAL)
    gh = user.connect("github", {"token": GH})
    assert zen["health"] == "verifying" and zen["credential"]["ordinal"] == 1
    stack.drain()
    listing = user.get(f"/api/workspaces/{user.workspace_id}/connections").json()["items"]
    by_kind = {c["kind"]: c for c in listing}
    assert {c["health"] for c in listing} == {"ready"}
    assert by_kind["github"]["external_identity"] == "octo-test"
    generic = by_kind["inference_api"]
    assert generic["validation"]["quota_consuming"] is True
    assert generic["legacy"] is False and generic["credential"]["format"] == "inference_api/v1"
    # Settings are visible; the key is not.
    assert generic["config"] == {
        "endpoints": {"openai_chat": INFERENCE_URL},
        "model": "test-model",
        "models": ["test-model"],
    }
    models = user.get(f"/api/models?workspace_id={user.workspace_id}").json()
    assert models["preferred_model"] == "test-model"
    assert models["connections"][0]["models"][0] == {"id": "test-model", "efforts": []}
    assert models["connections"][0]["compatible"] is True
    assert models["connections"][0]["protocol"] == "openai_chat"
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
    zen = user.connect("inference_api", inference(ZEN))
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
        {"credential": {"api_key": "other-key-99999"}, "expected_version": zen["version"]},
    )
    assert stale_version.json()["error"]["code"] == "version_conflict"
    from control.jobs.worker import JobContext

    ctx = JobContext(stack.db, claim, stack.worker)
    assert stack.services.connections.handle_validate(ctx).result["skipped"] == "stale_or_revoked"
    stack.drain()
    current = user.get(f"/api/connections/{zen['id']}").json()
    assert current["health"] == "reauth_required" and current["credential"]["ordinal"] == 2
    assert current["config"]["endpoints"] == {"openai_chat": INFERENCE_URL}, (
        "rotating only the key keeps the endpoints and model"
    )
    con = stack.db.read(lambda u: u.get("connections", zen["id"]))
    assert con["revocation_epoch"] == 2


def test_validation_racing_replacement_commit_is_discarded(stack) -> None:
    user = User(stack)
    zen = user.connect("inference_api", inference(ZEN))
    real = stack.services.connections.validators["inference_api"]

    def racing(material, **kw):
        stack.services.connections.validators["inference_api"] = real
        user.post(
            f"/api/connections/{zen['id']}/credential-versions",
            {"credential": {"api_key": "invalid-race-key-123"}, "expected_version": zen["version"]},
        )
        return real(material, **kw)

    stack.services.connections.validators["inference_api"] = racing
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
    zen = user.connect("inference_api", inference(ZEN))
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
    assert r.json()["error"]["details"]["kind"] == "inference_api"
    user.connect("inference_api", inference(ZEN))
    stack.drain()
    r = user.post(
        f"/api/workspaces/{user.workspace_id}/sessions",
        {"harness": {"provider_id": "opencode"}, "executor": {"backend": "modal"}},
    )
    assert r.json()["error"]["details"]["kind"] == "modal"
    assert os.environ["MODAL_TOKEN_ID"] == "ak-ambient"


def test_cross_owner_isolation(stack) -> None:
    alice, bob = User(stack), User(stack)
    zen = alice.connect("inference_api", inference(ZEN))
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
    zen = user.connect("inference_api", inference(ZEN))
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
    assert bundle == {"inference": {"api_key": ZEN}} and meta["connection_id"] == zen["id"]
    assert meta["inference"] == {
        "protocol": "openai_chat",
        "base_url": INFERENCE_URL,
        "model": "test-model",
    }
    wrong = build_services(
        make_config(db, tmp_path), validators=fake_validators(), executors={}, db=db
    )
    with pytest.raises(VaultError):
        wrong.broker.inference(session)


def test_vault_binds_associated_data_and_rotates() -> None:
    old = Vault.from_spec(Vault.generate_spec("k1"))
    aad_a = Vault.aad("wsp_a", "con_a", "cred_a", "inference_api/v1")
    aad_b = Vault.aad("wsp_a", "con_b", "cred_a", "inference_api/v1")
    sealed = old.seal({"api_key": ZEN}, aad_a)
    with pytest.raises(VaultError):
        old.open(sealed, aad_b)
    rotated = Vault({"k2": os.urandom(32), "k1": old.keyring["k1"]}, "k2")
    assert rotated.open(sealed, aad_a)["api_key"] == ZEN
    assert rotated.seal({"x": "y"}, aad_a).key_id == "k2"


RESPONSES_URL = "https://inference.example.test/responses-api/v1"
ANTHROPIC_URL = "https://inference.example.test/anthropic"


def _session(user: User, provider: str, **extra) -> object:
    return user.post(
        f"/api/workspaces/{user.workspace_id}/sessions",
        {"harness": {"provider_id": provider}, "executor": {"backend": "local"}, **extra},
    )


def test_harness_and_connection_are_matched_by_protocol(stack) -> None:
    """The Harness is decoupled from the provider: any Connection offering a protocol the
    official CLI speaks can drive it, and one that offers none is refused with the reason."""
    user = User(stack)
    chat = user.connect("inference_api", inference(ZEN), "chat only")
    stack.drain()
    for provider in ("opencode", "grok", "commandcode"):
        created = _session(user, provider)
        assert created.status_code == 201, created.text
        session = created.json()["session"]
        assert session["connections"]["inference"] == chat["id"]
        assert session["harness"] == {
            "provider_id": provider,
            "model": "test-model",
            "effort": None,
        }
    for provider, protocols in (
        ("codex", ["openai_responses"]),
        ("claude", ["anthropic_messages"]),
    ):
        refused = _session(user, provider)
        assert refused.status_code == 409, refused.text
        error = refused.json()["error"]
        assert error["code"] == "connection_required"
        assert error["details"]["inference_protocols"] == protocols
        explicit = _session(user, provider, connections={"inference": chat["id"]})
        assert explicit.status_code == 422
        assert explicit.json()["error"]["details"]["field"] == "connections.inference"
        listed = user.get(f"/api/models?provider_id={provider}").json()
        assert listed["inference_protocols"] == protocols and listed["preferred_model"] is None
        assert [c["compatible"] for c in listed["connections"]] == [False]
    multi = user.connect(
        "inference_api",
        {
            "api_key": "multi-protocol-key-000001",
            "model": "multi-model",
            "models": ["multi-model-large"],
            "endpoints": {
                "openai_chat": INFERENCE_URL,
                "openai_responses": RESPONSES_URL,
                "anthropic_messages": ANTHROPIC_URL,
            },
        },
        "all protocols",
    )
    stack.drain()
    expected = {
        "codex": ("openai_responses", RESPONSES_URL),
        "claude": ("anthropic_messages", ANTHROPIC_URL),
    }
    for provider, (protocol, url) in expected.items():
        created = _session(user, provider)
        assert created.status_code == 201, created.text
        body = created.json()["session"]
        assert body["connections"]["inference"] == multi["id"]
        assert body["harness"]["model"] == "multi-model"
        session = stack.db.read(lambda u, sid=body["id"]: u.get("sessions", sid))
        bundle, meta = stack.services.broker.inference(session)
        assert bundle == {"inference": {"api_key": "multi-protocol-key-000001"}}
        assert meta["inference"] == {"protocol": protocol, "base_url": url, "model": "multi-model"}
    # A caller-chosen model is pinned as given; the Connection's default is only a default.
    chosen = _session(
        user, "claude", harness={"provider_id": "claude", "model": "multi-model-large"}
    )
    assert chosen.json()["session"]["harness"]["model"] == "multi-model-large"


@pytest.mark.parametrize(
    "credential,field",
    [
        (
            {"api_key": "k" * 20, "base_url": "http://inference.example.test/v1", "model": "m"},
            "endpoints.openai_chat",
        ),
        (
            {"api_key": "k" * 20, "base_url": "https://127.0.0.1/v1", "model": "m"},
            "endpoints.openai_chat",
        ),
        (
            {"api_key": "k" * 20, "base_url": "https://169.254.169.254/latest", "model": "m"},
            "endpoints.openai_chat",
        ),
        (
            {"api_key": "k" * 20, "base_url": "https://localhost/v1", "model": "m"},
            "endpoints.openai_chat",
        ),
        (
            {"api_key": "k" * 20, "base_url": "https://[::1]/v1", "model": "m"},
            "endpoints.openai_chat",
        ),
        (
            {
                "api_key": "k" * 20,
                "base_url": "https://user:pw@inference.example.test/v1",
                "model": "m",
            },
            "endpoints.openai_chat",
        ),
        (
            {
                "api_key": "k" * 20,
                "base_url": "https://inference.example.test/v1?x=1",
                "model": "m",
            },
            "endpoints.openai_chat",
        ),
        (
            {"api_key": "k" * 20, "base_url": INFERENCE_URL, "protocol": "grpc", "model": "m"},
            "protocol",
        ),
        ({"api_key": "k" * 20, "base_url": INFERENCE_URL, "model": "bad model\n"}, "model"),
        ({"api_key": "k" * 20, "base_url": INFERENCE_URL}, "model"),
        ({"api_key": "k" * 20, "model": "m"}, "endpoints"),
        ({"base_url": INFERENCE_URL, "model": "m"}, "api_key"),
    ],
)
def test_inference_connection_input_is_validated_without_echo(stack, credential, field) -> None:
    user = User(stack)
    r = user.post(
        f"/api/workspaces/{user.workspace_id}/connections",
        {"kind": "inference_api", "credential": credential},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["field"] == f"credential.{field}"
    for value in credential.values():
        if isinstance(value, str) and len(value) > 6:
            assert value not in r.text, "input is never echoed"


def test_retired_vendor_kinds_are_refused_but_stored_ones_survive(stack) -> None:
    """New flows cannot create Zen/Codex credentials; rows written before the change keep
    decrypting, keep serving the Session pinned to them, and can still be disconnected."""
    user = User(stack)
    for kind, credential in (
        ("opencode_zen", {"api_key": "zen-retired-key-0000001"}),
        ("codex", {"auth_json": '{"OPENAI_API_KEY": "sk-retired"}'}),
    ):
        r = user.post(
            f"/api/workspaces/{user.workspace_id}/connections",
            {"kind": kind, "credential": credential},
        )
        assert r.status_code == 422 and r.json()["error"]["details"]["field"] == "kind"
        assert r.json()["error"]["details"]["replacement"] == "inference_api"
    # A Connection and Session exactly as the previous release stored them.
    vault, legacy_key = stack.services.connections.vault, "zen-stored-before-upgrade-01"
    con_id, version_id = new_id("connection"), new_id("credential_version")
    sealed = vault.seal(
        {"api_key": legacy_key}, vault.aad(user.workspace_id, con_id, version_id, "zen_api_key/v1")
    )
    owner = user.me["user"]["id"]

    def seed(u) -> None:
        u.insert(
            "connections",
            {
                "id": con_id,
                "workspace_id": user.workspace_id,
                "kind": "opencode_zen",
                "label": "old zen",
                "created_by": owner,
                "health": "ready",
            },
        )
        u.insert(
            "credential_versions",
            {
                "id": version_id,
                "workspace_id": user.workspace_id,
                "connection_id": con_id,
                "ordinal": 1,
                "format": "zen_api_key/v1",
                "key_id": sealed.key_id,
                "nonce": sealed.nonce,
                "ciphertext": sealed.ciphertext,
                "fingerprint": "f",
                "created_by": owner,
            },
        )
        u.update("connections", con_id, {"current_credential_version_id": version_id})

    stack.db.run(seed)
    listed = user.get(f"/api/workspaces/{user.workspace_id}/connections").json()["items"]
    assert [(c["kind"], c["legacy"], c["config"]) for c in listed] == [("opencode_zen", True, {})]
    # New Sessions never select a retired Connection, implicitly or explicitly.
    assert _session(user, "opencode").json()["error"]["code"] == "connection_required"
    explicit = _session(user, "opencode", connections={"inference": con_id})
    assert explicit.status_code == 422
    # A Session pinned before the upgrade still resolves its original credential lane.
    user.connect("inference_api", inference(ZEN))
    stack.drain()
    created = _session(user, "opencode").json()["session"]
    stack.db.run(lambda u: u.update("sessions", created["id"], {"inference_connection_id": con_id}))
    session = stack.db.read(lambda u: u.get("sessions", created["id"]))
    bundle, meta = stack.services.broker.inference(session)
    assert bundle == {"opencode_zen": {"api_key": legacy_key}} and "inference" not in meta
    replaced = user.post(
        f"/api/connections/{con_id}/credential-versions",
        {"credential": {"api_key": "zen-new-key-0000000001"}, "expected_version": 1},
    )
    assert replaced.status_code == 422 and replaced.json()["error"]["details"]["field"] == "kind"
    revoked = user.delete(f"/api/connections/{con_id}")
    assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
    assert legacy_key not in user.all_text()


class _PrewarmOnly:
    """Stands in for the Modal executor: records the image prewarm, creates nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple[set[str], str]] = []

    def prewarm(self, compute: dict, connection_key: str) -> dict:
        self.calls.append((set(compute), connection_key))
        return {"image_id": "im-test", "recipe_digest": "d1", "image_resolve_ms": 3}


def test_verified_modal_connection_prewarms_the_runtime_image_once(db, tmp_path) -> None:
    executor = _PrewarmOnly()
    stack = ApiStack(db, tmp_path, executors={"modal": executor})
    try:
        user = User(stack)
        modal = user.connect("modal", MODAL)
        user.connect("inference_api", inference(ZEN), "My Zen")
        stack.drain()
        assert executor.calls == [({"token_id", "token_secret"}, modal["id"])]
        jobs = db.read(lambda u: u.find("jobs", {"kind": "connection.provision"}))
        assert [j["state"] for j in jobs] == ["succeeded"]
        assert jobs[0]["result"]["image_id"] == "im-test"
        assert "token" not in str(jobs[0]["input"]) and MODAL["token_secret"] not in str(jobs[0])
        bad = user.connect("modal", {"token_id": "ak-bad0000000000001", "token_secret": "x" * 20})
        stack.drain()
        assert len(executor.calls) == 1, (
            f"an unverified credential ({bad['health']}) builds nothing"
        )
    finally:
        stack.shutdown()
