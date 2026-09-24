"""SOR-176: independent cloud credential lifecycle unit tests.

Covers the non-secret lifecycle record (states, kind classification, expiry
extraction, terminal transitions), the per-account refresh claim, the
api_key write-back skip, and the proactive ``CredentialRefresher`` over the
real ``LocalProcessBackend`` + ``stub_runner``. No Modal, no real
credentials — all token material is ``REDACTED``-shaped fixtures.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from control.accounts import (
    FileAccountStore,
    InMemoryAccountStore,
    PersistentAccountRegistry,
)
from control.backend import LocalProcessBackend, SandboxSpec
from control.credlifecycle import (
    CredentialLifecycleService,
    CredentialRefresher,
    credential_expiry_epoch,
    credential_kind,
    grant_revoked,
    provider_refresh_argv,
)
from control.credsync import (
    TAG_CRED_BASE_FP,
    CredentialSync,
    blob_fingerprint,
)
from control.ports import Account

ACCOUNT_ID = "acct-codex-176"
MANAGED_SECRET = f"sbx-acct-{ACCOUNT_ID}"
STUB_RUNNER = Path(__file__).resolve().parents[2] / "fakes" / "stub_runner.py"
RUNNER = [sys.executable, str(STUB_RUNNER)]


def _jwt(exp: int) -> str:
    def _b64(obj: dict) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{_b64({'alg': 'RS256'})}.{_b64({'exp': exp})}.REDACTED"


def _codex_blob(marker: str = "old", *, exp: int | None = None) -> dict[str, Any]:
    tokens: dict[str, Any] = {
        "access_token": _jwt(exp) if exp else f"REDACTED-{marker}",
        "refresh_token": "REDACTED-refresh",
        "account_id": "REDACTED-acct",
    }
    auth = {"auth_mode": "auth_json", "tokens": tokens, "last_refresh": "2026-09-01"}
    return {"provider": "codex", "files": {".codex/auth.json": json.dumps(auth)}}


def _opencode_api_blob() -> dict[str, Any]:
    # OpenCode Zen entry: ``type: api`` + a static key — no refresh channel.
    auth = {"zen": {"type": "api", "key": "REDACTED-zen"}}
    return {"provider": "opencode", "files": {".local/share/opencode/auth.json": json.dumps(auth)}}


def _opencode_oauth_blob(expires_ms: int | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "oauth",
        "refresh": "REDACTED-refresh",
        "access": "REDACTED-access",
    }
    if expires_ms is not None:
        entry["expires"] = expires_ms
    return {
        "provider": "opencode",
        "files": {".local/share/opencode/auth.json": json.dumps({"openai": entry})},
    }


def _devin_blob() -> dict[str, Any]:
    return {
        "provider": "devin",
        "files": {".local/share/devin/credentials.toml": 'windsurf_api_key = "REDACTED"'},
    }


def _account(provider: str = "codex", account_id: str = ACCOUNT_ID, **kw: Any) -> Account:
    return Account(
        id=account_id,
        provider=provider,
        label="176",
        secret_name=f"sbx-acct-{account_id}",
        created_at="2026-09-24T00:00:00+00:00",
        **kw,
    )


@pytest.fixture
def registry() -> PersistentAccountRegistry:
    reg = PersistentAccountRegistry(InMemoryAccountStore())
    reg.put(_account())
    return reg


# ---------------------------------------------------------------- classification


def test_credential_kind_oauth_vs_static() -> None:
    assert credential_kind("codex", _codex_blob()) == "oauth"
    assert credential_kind("opencode", _opencode_oauth_blob()) == "oauth"
    # OpenCode Zen / api entries and Devin key files are static material.
    assert credential_kind("opencode", _opencode_api_blob()) == "api_key"
    assert credential_kind("devin", _devin_blob()) == "api_key"
    # Unknown blobs stay conservative (oauth) so refresh can still harvest.
    assert credential_kind("codex", None) == "oauth"


def test_credential_expiry_epoch_per_provider() -> None:
    future = 4_000_000_000
    assert credential_expiry_epoch("codex", _codex_blob(exp=future)) == future

    expected_iso = int(datetime(2026, 9, 26, 3, 9, 6, tzinfo=UTC).timestamp())
    grok_entry = {
        "key": "REDACTED",
        "refresh_token": "R",
        "expires_at": "2026-09-26T03:09:06+00:00",
    }
    grok = {
        "provider": "grok",
        "files": {".grok/auth.json": json.dumps({"https://auth.x.ai::x": grok_entry})},
    }
    assert credential_expiry_epoch("grok", grok) == expected_iso

    agy_token = {"access_token": "R", "refresh_token": "R", "expiry": "2026-09-26T03:09:06Z"}
    agy = {
        "provider": "antigravity",
        "files": {".gemini/x": json.dumps({"token": agy_token})},
    }
    assert credential_expiry_epoch("antigravity", agy) == expected_iso

    # opencode ``expires`` is epoch milliseconds.
    assert credential_expiry_epoch("opencode", _opencode_oauth_blob(future * 1000)) == future
    # api-only blob carries no expiry signal.
    assert credential_expiry_epoch("opencode", _opencode_api_blob()) is None
    assert credential_expiry_epoch("devin", _devin_blob()) is None


def test_grant_revoked_markers() -> None:
    assert grant_revoked('{"code":"invalid_grant"}')
    assert grant_revoked("401 Unauthorized: invalid refresh token")
    assert grant_revoked("token_expired")
    assert not grant_revoked("401 Unauthorized: invalid or expired token")
    assert not grant_revoked(None)


def test_provider_refresh_argv_prefers_real_call_for_codex() -> None:
    argv = provider_refresh_argv("codex", env={"CODEX_BIN": "codex"})
    assert argv is not None
    assert argv[:2] == ["codex", "exec"]
    # devin's documented auth check is fine as-is.
    assert provider_refresh_argv("devin", env={"DEVIN_BIN": "devin"}) == [
        "devin",
        "auth",
        "status",
    ]
    assert provider_refresh_argv("opencode", model="openai/m", env={}) == [
        "opencode",
        "run",
        "-m",
        "openai/m",
        "Reply with exactly: ok",
    ]


# ---------------------------------------------------------------- service


def test_note_credential_healthy_and_kind(registry: PersistentAccountRegistry) -> None:
    registry.put_credential_blob(ACCOUNT_ID, _codex_blob())
    svc = CredentialLifecycleService(registry)
    rec = svc.note_credential(ACCOUNT_ID, _codex_blob())
    assert rec["state"] == "healthy"
    assert rec["kind"] == "oauth"
    assert rec["generation"] == 1
    desc = svc.describe(ACCOUNT_ID)
    assert desc["state"] == "healthy"
    assert desc["refreshable"] is True
    assert desc["secret_managed"] is True
    # Non-secret surface only.
    assert set(desc) <= {
        "account_id",
        "provider",
        "kind",
        "state",
        "expires_at",
        "fingerprint",
        "generation",
        "imported_at",
        "last_refresh_at",
        "last_refreshed_at",
        "last_error",
        "secret_managed",
        "refreshable",
        "refresh_due",
    }


def test_access_expiring_when_expiry_within_window(registry: PersistentAccountRegistry) -> None:
    svc = CredentialLifecycleService(registry)
    soon = int(time.time()) + 60  # inside the 30min window
    rec = svc.note_credential(ACCOUNT_ID, _codex_blob(exp=soon))
    assert rec["state"] == "access_expiring"
    assert svc.describe(ACCOUNT_ID)["expires_at"] == soon


def test_refresh_claim_serializes_and_recovers(registry: PersistentAccountRegistry) -> None:
    svc = CredentialLifecycleService(registry)
    assert svc.begin_refresh(ACCOUNT_ID) is True
    assert svc.describe(ACCOUNT_ID)["state"] == "refreshing"
    # A second claim while one is in flight is refused.
    assert svc.begin_refresh(ACCOUNT_ID) is False
    svc.finish_refresh(ACCOUNT_ID, outcome="ok")
    assert svc.begin_refresh(ACCOUNT_ID) is True
    # A crashed claim (stale timestamp) can be re-claimed.
    rec = registry.get_credential_lifecycle(ACCOUNT_ID) or {}
    rec["refresh_started_at"] = 0
    registry.put_credential_lifecycle(ACCOUNT_ID, rec)
    svc2 = CredentialLifecycleService(registry)
    assert svc2.begin_refresh(ACCOUNT_ID) is True


def test_on_auth_invalid_marks_account_and_state(registry: PersistentAccountRegistry) -> None:
    svc = CredentialLifecycleService(registry)
    svc.note_credential(ACCOUNT_ID, _codex_blob())
    svc.on_auth_invalid(ACCOUNT_ID, detail="401 Unauthorized")
    assert svc.describe(ACCOUNT_ID)["state"] == "reauth_required"
    assert registry.get(ACCOUNT_ID).status == "invalid"  # failover
    svc.on_auth_invalid(ACCOUNT_ID, detail='{"code":"invalid_grant"}')
    assert svc.describe(ACCOUNT_ID)["state"] == "revoked"
    # A *changed* credential import clears the terminal flag.
    svc.note_credential(ACCOUNT_ID, _codex_blob("reimported"))
    assert svc.describe(ACCOUNT_ID)["state"] == "healthy"


def test_finish_refresh_commit_bumps_generation(registry: PersistentAccountRegistry) -> None:
    svc = CredentialLifecycleService(registry)
    svc.note_credential(ACCOUNT_ID, _codex_blob())
    assert svc.begin_refresh(ACCOUNT_ID) is True
    rotated = _codex_blob("rotated", exp=4_000_000_000)
    rec = svc.finish_refresh(ACCOUNT_ID, outcome="committed", blob=rotated)
    assert rec["state"] == "healthy_refreshed"
    assert rec["generation"] == 2
    desc = svc.describe(ACCOUNT_ID)
    assert desc["state"] == "healthy_refreshed"
    assert desc["expires_at"] == 4_000_000_000
    assert desc["fingerprint"] == blob_fingerprint(rotated)


def test_terminal_state_sticky_until_new_fingerprint(registry: PersistentAccountRegistry) -> None:
    svc = CredentialLifecycleService(registry)
    blob = _codex_blob()
    svc.note_credential(ACCOUNT_ID, blob)
    svc.on_auth_invalid(ACCOUNT_ID, detail="invalid_grant")
    # Re-importing the *same* dead bundle must not clear the flag.
    svc.note_credential(ACCOUNT_ID, blob)
    assert svc.describe(ACCOUNT_ID)["state"] == "revoked"


def test_lifecycle_record_persists_in_file_store(tmp_path: Path) -> None:
    reg = PersistentAccountRegistry(FileAccountStore(tmp_path / "store"))
    reg.put(_account())
    reg.put_credential_blob(ACCOUNT_ID, _codex_blob())
    svc = CredentialLifecycleService(reg)
    svc.note_credential(ACCOUNT_ID, _codex_blob())
    # A fresh registry over the same dir sees the lifecycle record.
    reg2 = PersistentAccountRegistry(FileAccountStore(tmp_path / "store"))
    rec = reg2.get_credential_lifecycle(ACCOUNT_ID)
    assert rec is not None and rec["state"] == "healthy"
    reg2.remove(ACCOUNT_ID)
    assert reg2.get_credential_lifecycle(ACCOUNT_ID) is None


# ---------------------------------------------------------------- writeback hooks


def _cred_handle_env(monkeypatch: pytest.MonkeyPatch, blob: dict[str, Any]) -> Any:
    from control.sandbox_io import drain, sandbox_env

    backend = LocalProcessBackend()
    handle = backend.create(SandboxSpec(tags={"provider": "codex", "account_id": ACCOUNT_ID}))
    monkeypatch.setenv("SBX_ACCOUNT_ID", ACCOUNT_ID)
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", json.dumps(blob))
    init = backend.exec(
        handle,
        [
            *RUNNER,
            "init",
            "--auth",
            "auth_json",
            "--model",
            "gpt-5.6-luna",
            "--provider",
            "codex",
            "--account-id",
            ACCOUNT_ID,
        ],
        env=sandbox_env(handle),
    )
    assert drain(init) == 0
    return backend, handle


def test_writeback_commit_feeds_lifecycle(
    registry: PersistentAccountRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    blob = _codex_blob()
    registry.put_credential_blob(ACCOUNT_ID, blob)
    lifecycle = CredentialLifecycleService(registry)
    lifecycle.note_credential(ACCOUNT_ID, blob)
    sync = CredentialSync(lambda: registry, lifecycle=lifecycle)
    backend, handle = _cred_handle_env(monkeypatch, blob)
    try:
        # Simulate the CLI's own rotation inside the sandbox.
        auth_path = handle.root / "home" / ".codex" / "auth.json"
        rotated = _codex_blob("rotated")["files"][".codex/auth.json"] + "\n"
        auth_path.write_text(rotated, encoding="utf-8")
        outcome = sync.writeback(
            backend=backend,
            handle=handle,
            runner_cmd=RUNNER,
            tags={"account_id": ACCOUNT_ID, TAG_CRED_BASE_FP: blob_fingerprint(blob)},
        )
        assert outcome.code == "committed"
        desc = lifecycle.describe(ACCOUNT_ID)
        assert desc["state"] == "healthy_refreshed"
        assert desc["generation"] == 2
        assert desc["fingerprint"] == outcome.fingerprint
    finally:
        backend.terminate(handle)


def test_writeback_skipped_for_static_credential(
    registry: PersistentAccountRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.put(_account(provider="devin", account_id="acct-devin-s"))
    registry.put_credential_blob("acct-devin-s", _devin_blob())
    sync = CredentialSync(lambda: registry)
    backend, handle = _cred_handle_env(monkeypatch, _devin_blob())
    try:
        outcome = sync.writeback(
            backend=backend,
            handle=handle,
            runner_cmd=RUNNER,
            tags={
                "account_id": "acct-devin-s",
                TAG_CRED_BASE_FP: blob_fingerprint(_devin_blob()),
            },
        )
        # No refresh channel → no export exec, no mutation.
        assert outcome.code == "skipped:static_credential"
        assert registry.get_credential_blob("acct-devin-s") == _devin_blob()
    finally:
        backend.terminate(handle)


# ---------------------------------------------------------------- refresher


def _write_rotator(tmp_path: Path, mode: str) -> Path:
    """A stand-in provider CLI: 'rotate' rewrites the restored auth file the
    way a real refresh would, 'revoked' exits 1 with an invalid_grant error,
    'noop' succeeds without touching files (mode is baked in because sandbox
    execs only see the whitelisted env)."""
    script = tmp_path / f"rotator_{mode}.py"
    script.write_text(
        f"""import json, os, sys
from pathlib import Path
MODE = {mode!r}
if MODE == "revoked":
    print('{{"code":"invalid_grant","message":"refresh token revoked"}}')
    sys.exit(1)
auth = Path(os.environ["SBX_WORK"]) / "home" / ".codex" / "auth.json"
data = json.loads(auth.read_text())
if MODE == "rotate":
    data.setdefault("tokens", {{}})["access_token"] = "REDACTED-rotated"
    auth.write_text(json.dumps(data) + "\\n")
print("ok")
"""
    )
    return script


def _refresher(
    registry: PersistentAccountRegistry,
    *,
    spy: Any = None,
    lifecycle: CredentialLifecycleService | None = None,
) -> tuple[CredentialRefresher, LocalProcessBackend, CredentialSync]:
    backend = LocalProcessBackend()
    sync = CredentialSync(lambda: registry, secret_writer=spy)
    svc = lifecycle or CredentialLifecycleService(registry)
    return (
        CredentialRefresher(
            registry_source=lambda: registry,
            backend=backend,
            runner_cmd=RUNNER,
            sync=sync,
            lifecycle=svc,
        ),
        backend,
        sync,
    )


class _SpySecretWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def refresh(self, secret_name: str, env: dict[str, str]) -> None:
        self.calls.append((secret_name, dict(env)))


def test_refresher_rotates_commits_and_marks_healthy(
    registry: PersistentAccountRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blob = _codex_blob()
    registry.put_credential_blob(ACCOUNT_ID, blob)
    spy = _SpySecretWriter()
    lifecycle = CredentialLifecycleService(registry)
    lifecycle.note_credential(ACCOUNT_ID, blob)
    refresher, backend, _ = _refresher(registry, spy=spy, lifecycle=lifecycle)
    script = _write_rotator(tmp_path, "rotate")
    monkeypatch.setenv("CODEX_BIN", str(script))

    result = refresher.refresh_account(ACCOUNT_ID)

    assert result["result"] == "committed"
    stored = registry.get_credential_blob(ACCOUNT_ID)
    assert stored is not None and "REDACTED-rotated" in stored["files"][".codex/auth.json"]
    desc = lifecycle.describe(ACCOUNT_ID)
    assert desc["state"] == "healthy_refreshed"
    assert desc["generation"] == 2
    # Managed Secret was recreated with the rotated bundle.
    assert len(spy.calls) == 1
    assert spy.calls[0][0] == MANAGED_SECRET

    # New-sandbox-after-refresh persistence: a fresh sandbox seeded from the
    # store restores the rotated bundle — the write-back is authoritative.
    from control.sandbox_io import drain, sandbox_env

    handle2 = backend.create(SandboxSpec(tags={"provider": "codex", "account_id": ACCOUNT_ID}))
    monkeypatch.setenv("SBX_ACCOUNT_ID", ACCOUNT_ID)
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", json.dumps(stored))
    try:
        init = backend.exec(
            handle2,
            [
                *RUNNER,
                "init",
                "--auth",
                "auth_json",
                "--model",
                "gpt-5.6-luna",
                "--provider",
                "codex",
                "--account-id",
                ACCOUNT_ID,
            ],
            env=sandbox_env(handle2),
        )
        assert drain(init) == 0
        restored = (handle2.root / "home" / ".codex" / "auth.json").read_text()
        assert "REDACTED-rotated" in restored
    finally:
        backend.terminate(handle2)


def test_refresher_static_credential_never_runs(
    registry: PersistentAccountRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry.put(_account(provider="devin", account_id="acct-devin-r"))
    registry.put_credential_blob("acct-devin-r", _devin_blob())
    lifecycle = CredentialLifecycleService(registry)
    refresher, _, _ = _refresher(registry, lifecycle=lifecycle)
    monkeypatch.setenv("DEVIN_BIN", "definitely-not-installed")

    result = refresher.refresh_account("acct-devin-r")

    # Devin key credentials never acquire a refresh worker exec.
    assert result["result"] == "skipped:static_credential"
    assert lifecycle.describe("acct-devin-r")["kind"] == "api_key"


def test_refresher_invalid_grant_marks_revoked_and_invalid(
    registry: PersistentAccountRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry.put_credential_blob(ACCOUNT_ID, _codex_blob())
    lifecycle = CredentialLifecycleService(registry)
    lifecycle.note_credential(ACCOUNT_ID, _codex_blob())
    refresher, _, _ = _refresher(registry, lifecycle=lifecycle)
    script = _write_rotator(tmp_path, "revoked")
    monkeypatch.setenv("CODEX_BIN", str(script))

    result = refresher.refresh_account(ACCOUNT_ID)

    # invalid_grant → revoked + account invalid (failover), not a 401 loop.
    assert result["result"] == "revoked"
    assert lifecycle.describe(ACCOUNT_ID)["state"] == "revoked"
    assert registry.get(ACCOUNT_ID).status == "invalid"
    # And a later scan refuses to spend another refresh on it.
    assert refresher.refresh_account(ACCOUNT_ID)["result"] == "skipped:terminal"


def test_local_cli_file_untouched_by_cloud_refresh(
    registry: PersistentAccountRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Local CLI credential files are import sources only.

    The cloud lane writes the store blob + managed Secret; the file the blob
    was imported from stays byte-identical and the local CLI still works.
    """
    blob = _codex_blob()
    registry.put_credential_blob(ACCOUNT_ID, blob)
    lifecycle = CredentialLifecycleService(registry)
    lifecycle.note_credential(ACCOUNT_ID, blob)
    refresher, backend, _ = _refresher(registry, lifecycle=lifecycle)
    script = _write_rotator(tmp_path, "rotate")
    monkeypatch.setenv("CODEX_BIN", str(script))

    # The "local" auth file the credential was imported from.
    local_codex = tmp_path / "local-cli-home" / ".codex"
    local_codex.mkdir(parents=True)
    local_auth = local_codex / "auth.json"
    original = _codex_blob()["files"][".codex/auth.json"]
    local_auth.write_text(original, encoding="utf-8")
    before = local_auth.read_bytes()

    result = refresher.refresh_account(ACCOUNT_ID)
    assert result["result"] == "committed"
    assert registry.get_credential_blob(ACCOUNT_ID) != blob  # cloud rotated

    # The local file is byte-identical — cloud refresh never writes to it.
    assert local_auth.read_bytes() == before

    # Proof the local CLI still works against its untouched file: fake codex
    # ``login status`` reflects the restored credential under CODEX_HOME.
    from control.onboarding import provider_auth_argv

    fake_codex = Path(__file__).resolve().parents[2] / "fakes" / "fake_codex.py"
    argv = provider_auth_argv("codex", env={"CODEX_BIN": str(fake_codex)})
    assert argv is not None
    handle = backend.create(SandboxSpec(tags={"provider": "codex"}))
    try:
        from control.sandbox_io import drain

        proc = backend.exec(
            handle, argv, env={"CODEX_HOME": str(local_codex), "SBX_WORK": str(handle.root)}
        )
        assert drain(proc) == 0
    finally:
        backend.terminate(handle)
