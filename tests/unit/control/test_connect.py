"""SOR-214: Provider Connect service — hosted/pair lanes over AuthService."""

from __future__ import annotations

import hashlib
import json
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from control.connect import (
    CONNECT_STATES,
    ConnectError,
    ConnectSession,
    FileConnectStore,
    InMemoryConnectStore,
    ProviderConnectService,
    plane_verify,
    probe_account_credential,
    session_from_dict,
)
from control.provider_auth import AUTH_SESSION_STATES
from tests.fakes.fake_ports import InMemoryAccountRegistry


def _iso() -> str:
    return datetime.now(UTC).isoformat()


FAKE_HOME = Path(__file__).resolve().parents[2] / "fakes"
CODEX_BIN = FAKE_HOME / "fake_codex.py"


@pytest.fixture
def registry() -> InMemoryAccountRegistry:
    return InMemoryAccountRegistry()


@pytest.fixture
def service(registry: InMemoryAccountRegistry, tmp_path: Path) -> ProviderConnectService:
    return ProviderConnectService(
        registry,
        env={"SBX_BACKEND": "local", "HOME": str(tmp_path / "op-home")},
        work_dir=tmp_path / "connect",
        hosted_available=lambda _provider: True,
    )


def _wait_state(
    service: ProviderConnectService, sid: str, *states: str, timeout: float = 10.0
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = service.get(sid)
        if out["state"] in states:
            return out
        time.sleep(0.05)
    raise AssertionError(f"connect session {sid} stuck at {service.get(sid)['state']}")


def test_connect_states_extend_auth_states() -> None:
    assert set(AUTH_SESSION_STATES) <= set(CONNECT_STATES)
    assert {"failed", "cancelled", "expired"} <= set(CONNECT_STATES)


def test_public_payload_never_carries_credential(service: ProviderConnectService) -> None:
    sess = service.begin("codex", label="me", verify=lambda _a: True)
    public = service.get(sess["id"])
    assert set(public) <= {
        "id",
        "provider",
        "kind",
        "state",
        "account_id",
        "relink",
        "label",
        "browser_url",
        "user_code",
        "error",
        "created_at",
        "expires_at",
        "updated_at",
        "pair_ticket",
        "pair_command",
    }


def test_hosted_lane_login_writes_url_code_and_verifies(
    service: ProviderConnectService, registry: InMemoryAccountRegistry, tmp_path: Path
) -> None:
    """The launcher seam stands in for the provider CLI: it writes the
    declared credential into the scratch home and prints a device URL."""

    def launcher(argv: list[str], env: dict[str, str], on_output) -> int:
        assert env["HOME"].startswith(str(tmp_path / "connect" / "home-"))
        on_output("Open https://sbx.invalid/device\n")
        on_output("Enter code FAKE-1234\n")
        cred = Path(env["HOME"]) / ".codex" / "auth.json"
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text(json.dumps({"token": "REDACTED"}))
        cred.chmod(0o600)
        return 0

    service._launcher = launcher
    sess = service.begin("codex", label="codex-one", verify=lambda _aid: True)
    assert sess["kind"] == "hosted"
    out = _wait_state(service, sess["id"], "verified", "materialized", "failed")
    assert out["state"] == "verified"
    assert out["browser_url"] == "https://sbx.invalid/device"
    assert out["user_code"] == "FAKE-1234"
    account = registry.get(out["account_id"])
    assert account is not None
    assert account.provider == "codex"
    # Canonical engine drove the import — the account's auth_state is a
    # real AUTH_SESSION_STATES value, not a session-only state.
    auth_session = service._auth.session(account.id)
    assert auth_session.auth_state in AUTH_SESSION_STATES


def test_hosted_lane_real_popen_against_fake_cli(
    registry: InMemoryAccountRegistry, tmp_path: Path
) -> None:
    """End-to-end hosted login against the fake codex CLI binary."""
    service = ProviderConnectService(
        registry,
        env={
            "SBX_BACKEND": "local",
            "HOME": str(tmp_path / "op-home"),
            "CODEX_BIN": str(CODEX_BIN),
        },
        work_dir=tmp_path / "connect",
        hosted_available=lambda _p: True,
    )
    sess = service.begin("codex", verify=lambda _a: True)
    out = _wait_state(service, sess["id"], "verified", "materialized", "failed")
    assert out["state"] == "verified", out.get("error")
    assert out["browser_url"] == "https://sbx.invalid/device"
    assert out["user_code"] == "FAKE-1234"
    blob = registry.get_credential_blob(out["account_id"])
    assert blob is not None
    assert any("auth.json" in rel for rel in blob["files"])
    # The scratch home carried the credential — the operator $HOME did not.
    assert not (tmp_path / "op-home" / ".codex").exists()


def test_hosted_lane_login_failure_marks_failed(service: ProviderConnectService) -> None:
    service._launcher = lambda argv, env, on_output: 3
    sess = service.begin("codex")
    out = _wait_state(service, sess["id"], "failed")
    assert out["state"] == "failed"
    assert "login_failed" in (out["error"] or "")


def test_cancel_terminates_running_login(service: ProviderConnectService) -> None:
    def launcher(argv: list[str], env: dict[str, str], on_output) -> int:
        time.sleep(30)
        return 0

    service._launcher = launcher
    sess = service.begin("codex")
    out = service.cancel(sess["id"])
    assert out["state"] == "cancelled"
    assert service.cancel(sess["id"])["state"] == "cancelled"  # idempotent


def test_retry_spawns_a_fresh_session(service: ProviderConnectService) -> None:
    service._launcher = lambda argv, env, on_output: 7
    sess = service.begin("codex")
    _wait_state(service, sess["id"], "failed")
    service._launcher = lambda argv, env, on_output: 5
    fresh = service.retry(sess["id"])
    assert fresh["id"] != sess["id"]
    assert fresh["state"] == "authenticating"
    _wait_state(service, fresh["id"], "failed")


def test_retry_on_active_session_conflict(service: ProviderConnectService) -> None:
    service._launcher = lambda argv, env, on_output: time.sleep(30) and 0
    sess = service.begin("codex")
    try:
        with pytest.raises(ConnectError) as err:
            service.retry(sess["id"])
        assert err.value.code == "session_active"
        assert err.value.status_code == 409
    finally:
        service.cancel(sess["id"])


def test_relink_requires_existing_account(
    service: ProviderConnectService, registry: InMemoryAccountRegistry
) -> None:
    with pytest.raises(ConnectError) as err:
        service.begin("codex", account_id="acct-missing")
    assert err.value.code == "account_not_found"
    assert err.value.status_code == 404

    from control.ports import Account

    registry.put(Account(id="acct-g", provider="grok", label="g", created_at=_iso()))
    with pytest.raises(ConnectError) as err:
        service.begin("codex", account_id="acct-g")
    assert err.value.code == "provider_mismatch"


# ---------------------------------------------------------------------------
# pair lane


@pytest.fixture
def pair_service(registry: InMemoryAccountRegistry, tmp_path: Path) -> ProviderConnectService:
    # Pure-cloud posture: no provider CLI on the plane → every provider
    # takes the pair lane.
    return ProviderConnectService(
        registry,
        env={"SBX_BACKEND": "modal", "HOME": str(tmp_path / "op-home")},
        work_dir=tmp_path / "connect",
        hosted_available=lambda _provider: False,
    )


def _blob(provider: str = "codex") -> dict[str, Any]:
    files = {
        "codex": {".codex/auth.json": '{"token": "REDACTED"}'},
        "devin": {".local/share/devin/credentials.toml": 'token = "REDACTED"'},
        "antigravity": {".gemini/antigravity-cli/antigravity-oauth-token": '{"token": "REDACTED"}'},
        "grok": {".grok/auth.json": '{"token": "REDACTED"}'},
        "opencode": {".local/share/opencode/auth.json": '{"token": "REDACTED"}'},
        "claude": {".claude/.credentials.json": '{"token": "REDACTED"}'},
    }
    return {"provider": provider, "files": files[provider]}


def test_pair_lane_mints_single_use_ticket(pair_service: ProviderConnectService) -> None:
    sess = pair_service.begin("codex")
    assert sess["kind"] == "pair"
    assert sess["state"] == "authenticating"
    ticket = sess["pair_ticket"]
    assert ticket.startswith("sbxp_")
    assert sess["pair_command"] == f"sbx auth pair {ticket}"

    info = pair_service.pair_info(ticket)
    assert info["provider"] == "codex"
    assert info["session_id"] == sess["id"]
    # Lookup is non-consuming — a second read still works.
    assert pair_service.pair_info(ticket)["session_id"] == sess["id"]


def test_pair_ticket_stored_hashed_not_raw(pair_service: ProviderConnectService) -> None:
    sess = pair_service.begin("codex")
    digest = hashlib.sha256(sess["pair_ticket"].encode()).hexdigest()
    assert pair_service._store.get_ticket(digest) is not None
    # The raw ticket never lands in the ticket index.
    assert pair_service._store.get_ticket(sess["pair_ticket"]) is None


def test_pair_complete_imports_and_consumes_ticket(
    pair_service: ProviderConnectService, registry: InMemoryAccountRegistry
) -> None:
    sess = pair_service.begin("codex", label="mine", verify=lambda _a: True)
    ticket = sess["pair_ticket"]
    outcome = pair_service.complete_pair(ticket, _blob("codex"))
    assert outcome["verified"] is True
    assert outcome["connect"]["state"] == "verified"
    assert outcome["session"]["auth_state"] in AUTH_SESSION_STATES
    account = registry.get(outcome["account_id"])
    assert account is not None and account.label == "mine"
    # Single-use: a second completion with the same ticket is rejected.
    with pytest.raises(ConnectError) as err:
        pair_service.complete_pair(ticket, _blob("codex"))
    assert err.value.code == "pair_invalid"
    assert err.value.status_code == 401


def test_pair_complete_bad_blob_does_not_consume_ticket(
    pair_service: ProviderConnectService,
) -> None:
    sess = pair_service.begin("codex")
    ticket = sess["pair_ticket"]
    with pytest.raises(ConnectError) as err:
        pair_service.complete_pair(ticket, {"provider": "codex", "files": {}})
    assert err.value.status_code == 400
    # Ticket survived — a corrected blob still redeems it.
    outcome = pair_service.complete_pair(ticket, _blob("codex"), verify=lambda _a: False)
    assert outcome["connect"]["state"] == "materialized"


def test_pair_ticket_wrong_provider_blob_rejected(
    pair_service: ProviderConnectService,
) -> None:
    sess = pair_service.begin("codex")
    with pytest.raises(ConnectError):
        pair_service.complete_pair(sess["pair_ticket"], _blob("grok"))


def test_pair_cancel_drops_ticket(pair_service: ProviderConnectService) -> None:
    sess = pair_service.begin("codex")
    pair_service.cancel(sess["id"])
    with pytest.raises(ConnectError) as err:
        pair_service.pair_info(sess["pair_ticket"])
    assert err.value.code == "pair_invalid"


def test_pair_expiry(registry: InMemoryAccountRegistry, tmp_path: Path) -> None:
    clock = [1000.0]
    service = ProviderConnectService(
        registry,
        env={"SBX_BACKEND": "modal", "HOME": str(tmp_path / "op-home")},
        work_dir=tmp_path / "connect",
        hosted_available=lambda _p: False,
        clock=lambda: clock[0],
        ttl_s=60.0,
    )
    sess = service.begin("codex")
    ticket = sess["pair_ticket"]
    clock[0] += 3600
    with pytest.raises(ConnectError) as err:
        service.pair_info(ticket)
    assert err.value.code == "pair_invalid"
    # The session itself is expired on next read.
    assert service.get(sess["id"])["state"] == "expired"


def test_pair_relink_targets_existing_account(
    pair_service: ProviderConnectService, registry: InMemoryAccountRegistry
) -> None:
    from control.ports import Account

    registry.put(
        Account(
            id="acct-c1",
            provider="codex",
            label="old",
            status="invalid",
            created_at=_iso(),
        )
    )
    sess = pair_service.begin("codex", account_id="acct-c1", verify=lambda _a: True)
    assert sess["relink"] is True
    outcome = pair_service.complete_pair(sess["pair_ticket"], _blob("codex"))
    assert outcome["account_id"] == "acct-c1"
    assert outcome["verified"] is True


def test_list_sessions_and_unknown_id(pair_service: ProviderConnectService) -> None:
    pair_service.begin("codex")
    pair_service.begin("grok")
    listed = pair_service.list()
    assert len(listed) == 2
    with pytest.raises(ConnectError) as err:
        pair_service.get("conn-nope")
    assert err.value.status_code == 404


# ---------------------------------------------------------------------------
# stores


@pytest.mark.parametrize("store_cls", [InMemoryConnectStore])
def test_session_roundtrip(store_cls) -> None:
    store = store_cls()
    sess = ConnectSession(
        id="conn-1",
        provider="codex",
        kind="pair",
        state="authenticating",
        pair_ticket="sbxp_x",
        created_at=_iso(),
        expires_at=1.0,
        updated_at=_iso(),
    )
    store.put(sess)
    got = store.get("conn-1")
    assert got is not None and got.provider == "codex" and got.kind == "pair"
    got.state = "verified"
    store.put(got)
    assert store.get("conn-1").state == "verified"
    store.delete("conn-1")
    assert store.get("conn-1") is None


def test_file_store_roundtrip_and_ticket_hash(tmp_path: Path) -> None:
    store = FileConnectStore(tmp_path / "conn")
    digest = hashlib.sha256(b"sbxp_tok").hexdigest()
    store.put_ticket(digest, "conn-9", 123.0)
    assert store.get_ticket(digest) == ("conn-9", 123.0)
    # tickets.json holds the digest, never the raw ticket.
    raw = json.loads((tmp_path / "conn" / "tickets.json").read_text())
    assert "sbxp_tok" not in (tmp_path / "conn" / "tickets.json").read_text()
    assert list(raw) == [digest]
    assert store.pop_ticket(digest) == ("conn-9", 123.0)
    assert store.get_ticket(digest) is None


def test_session_from_dict_rejects_garbage() -> None:
    assert session_from_dict(None) is None
    assert session_from_dict({"id": 1}) is None
    assert session_from_dict("nope") is None


# ---------------------------------------------------------------------------
# verify probe


def test_probe_account_credential_marks_active(
    registry: InMemoryAccountRegistry, tmp_path: Path
) -> None:
    from control.backend import SandboxHandle
    from control.ports import Account

    registry.put(
        Account(id="acct-p", provider="codex", label="p", status="unverified", secret_name="test-account-secret", created_at=_iso())
    )

    class _Proc:
        stdout = iter(())

        def wait(self) -> int:
            return 0

    class _Backend:
        def create(self, spec: Any) -> SandboxHandle:
            return SandboxHandle(id="h1", root=tmp_path, tags=dict(spec.tags))

        def exec(self, handle: Any, argv: list[str], env: dict | None = None) -> _Proc:
            assert "init" in argv
            return _Proc()

        def terminate(self, handle: Any) -> None:
            pass

    class _Plane:
        backend = _Backend()

        def runner(self, *args: str) -> list[str]:
            return list(args)

    updated = probe_account_credential(_Plane(), registry, registry.get("acct-p"))
    assert updated.status == "active"


def test_probe_no_backend_keeps_status(registry: InMemoryAccountRegistry) -> None:
    from control.ports import Account

    registry.put(
        Account(id="acct-nb", provider="codex", label="n", status="unverified", created_at=_iso())
    )

    class _Plane:
        backend = None
        runner = None

    updated = probe_account_credential(_Plane(), registry, registry.get("acct-nb"))
    assert updated.status == "unverified"
    assert updated.last_error == "probe_unavailable"


def test_plane_verify_closure(registry: InMemoryAccountRegistry) -> None:
    from control.ports import Account

    registry.put(
        Account(id="acct-v", provider="codex", label="v", status="unverified", created_at=_iso())
    )
    fn = plane_verify(object(), registry)  # no backend attrs → probe_unavailable
    assert fn("acct-v") is False
    assert fn("acct-missing") is False


def test_blob_file_written_0600(pair_service: ProviderConnectService, tmp_path: Path) -> None:
    """Materialization must never leave a world-readable blob file."""
    captured: list[Path] = []
    import control.connect as conn_mod

    orig = conn_mod._write_blob_file

    def spy(path: Path, blob: dict) -> None:
        orig(path, blob)
        captured.append(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    conn_mod._write_blob_file = spy  # type: ignore[assignment]
    try:
        sess = pair_service.begin("codex")
        pair_service.complete_pair(sess["pair_ticket"], _blob("codex"), verify=lambda _a: False)
    finally:
        conn_mod._write_blob_file = orig  # type: ignore[assignment]
    # The blob file is always cleaned up after materialization.
    assert captured
    for path in captured:
        assert not path.exists()


def test_pair_secret_leak_no_raw_ticket_in_store(
    registry: InMemoryAccountRegistry, tmp_path: Path
) -> None:
    """File store on disk must not contain the redeemable ticket."""
    store = FileConnectStore(tmp_path / "conn")
    service = ProviderConnectService(
        registry,
        env={"SBX_BACKEND": "modal", "HOME": str(tmp_path / "op-home")},
        work_dir=tmp_path / "connect",
        hosted_available=lambda _p: False,
        store=store,
    )
    sess = service.begin("codex")
    raw_files = "\n".join(p.read_text() for p in (tmp_path / "conn").glob("*.json") if p.is_file())
    assert sess["pair_ticket"] not in raw_files
    # And blob materialization leaves no stray credential file behind.
    service.complete_pair(sess["pair_ticket"], _blob("codex"), verify=lambda _a: False)
    raw_files = "\n".join(p.read_text() for p in (tmp_path / "conn").glob("*.json") if p.is_file())
    assert "REDACTED" not in raw_files


def test_probe_without_account_credential_does_not_use_deployment_default(registry):
    from control.ports import Account
    from unittest.mock import Mock
    registry.put(Account(id="acct-empty", provider="codex", label="empty", status="unverified"))
    plane = Mock()
    updated = probe_account_credential(plane, registry, registry.get("acct-empty"))
    assert updated.status == "unverified"
    assert updated.last_error == "credential_missing"
    plane.backend.create.assert_not_called()
