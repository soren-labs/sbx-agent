"""SOR-147 (R011 WP-H1): credential export/write-back unit tests.

Runs against the real ``LocalProcessBackend`` + ``stub_runner`` (which
implements the frozen ``runner export-credentials`` contract) over an
``InMemoryAccountStore``-backed registry — no Modal, no credentials.
All token material is ``REDACTED``-shaped fixture content only.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from control.accounts import InMemoryAccountStore, PersistentAccountRegistry
from control.backend import LocalProcessBackend, SandboxSpec
from control.credsync import (
    TAG_CRED_BASE_FP,
    TAG_CRED_RUN_FP,
    CredentialSync,
    blob_fingerprint,
    credential_rotated,
)
from control.ports import Account
from control.sandbox_io import drain, sandbox_env
from control.service import ControlPlane
from control.store import InMemoryStore

ACCOUNT_ID = "acct-codex-h"
MANAGED_SECRET = f"sbx-acct-{ACCOUNT_ID}"
STUB_RUNNER = Path(__file__).resolve().parents[2] / "fakes" / "stub_runner.py"
RUNNER = [sys.executable, str(STUB_RUNNER)]


def _blob(marker: str) -> dict[str, Any]:
    """A valid codex credential blob; ``marker`` makes fingerprints differ."""
    auth = {
        "auth_mode": "auth_json",
        "tokens": {"access_token": f"REDACTED-{marker}", "refresh_token": "REDACTED"},
    }
    return {"provider": "codex", "files": {".codex/auth.json": json.dumps(auth)}}


class _SpySecretWriter:
    """Records refresh calls; captures env keys and a fingerprint only."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def refresh(self, secret_name: str, env: dict[str, str]) -> None:
        self.calls.append((secret_name, dict(env)))


class _CredEnv:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, secret_name: str = MANAGED_SECRET):
        self.registry = PersistentAccountRegistry(InMemoryAccountStore())
        self.registry.put(
            Account(
                id=ACCOUNT_ID,
                provider="codex",
                label="h",
                secret_name=secret_name,
                created_at="2026-09-19T00:00:00+00:00",
            )
        )
        self.registry.put_credential_blob(ACCOUNT_ID, _blob("old"))
        self.backend = LocalProcessBackend()
        self.handle = self.backend.create(
            SandboxSpec(tags={"provider": "codex", "account_id": ACCOUNT_ID})
        )
        # Ambient scoping: the blob + account id are forwarded into this
        # sandbox's execs only because both match the handle's tags.
        monkeypatch.setenv("SBX_ACCOUNT_ID", ACCOUNT_ID)
        monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", json.dumps(_blob("old")))
        init = self.backend.exec(
            self.handle,
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
            env=sandbox_env(self.handle),
        )
        assert drain(init) == 0
        self.writer = _SpySecretWriter()
        self.sync = CredentialSync(lambda: self.registry, secret_writer=self.writer)

    @property
    def base_fp(self) -> str:
        return blob_fingerprint(self.registry.get_credential_blob(ACCOUNT_ID)) or ""

    @property
    def tags(self) -> dict[str, str]:
        return {"account_id": ACCOUNT_ID, TAG_CRED_BASE_FP: self.base_fp}

    def rotate(self, marker: str = "new") -> dict[str, Any]:
        """Rewrite the restored credential file the way a refreshed CLI would.

        Returns the blob ``export-credentials`` will emit — the runner
        captures the file content verbatim (including the trailing newline).
        """
        auth_path = self.handle.root / "home" / ".codex" / "auth.json"
        content = _blob(marker)["files"][".codex/auth.json"] + "\n"
        auth_path.write_text(content, encoding="utf-8")
        return {"provider": "codex", "files": {".codex/auth.json": content}}

    def writeback(self, tags: dict[str, str] | None = None):
        return self.sync.writeback(
            backend=self.backend,
            handle=self.handle,
            runner_cmd=RUNNER,
            tags=self.tags if tags is None else tags,
        )

    def close(self) -> None:
        self.backend.terminate(self.handle)


@pytest.fixture
def cred_env(monkeypatch: pytest.MonkeyPatch) -> _CredEnv:
    env = _CredEnv(monkeypatch)
    yield env
    env.close()


def test_blob_fingerprint_is_canonical() -> None:
    a = {"provider": "codex", "files": {"x": "1", "y": "2"}}
    b = {"files": {"y": "2", "x": "1"}, "provider": "codex"}
    assert blob_fingerprint(a) == blob_fingerprint(b)
    assert blob_fingerprint({"files": {"x": "9"}}) != blob_fingerprint(a)
    assert blob_fingerprint(None) is None
    assert blob_fingerprint("nope") is None


def test_seed_fingerprint_matches_stored_blob(cred_env: _CredEnv) -> None:
    assert cred_env.sync.seed_fingerprint(ACCOUNT_ID) == cred_env.base_fp
    assert cred_env.sync.seed_fingerprint("auto") is None
    assert cred_env.sync.seed_fingerprint(None) is None


def test_seed_blob_returns_stored_blob(cred_env: _CredEnv) -> None:
    """``seed_blob`` hands the authoritative stored blob to the provision
    path — ``None`` for unusable accounts or when no blob is stored."""
    assert cred_env.sync.seed_blob(ACCOUNT_ID) == _blob("old")
    assert cred_env.sync.seed_blob("auto") is None
    assert cred_env.sync.seed_blob(None) is None
    assert cred_env.sync.seed_blob("acct-codex-unknown") is None


def test_provision_seeds_registry_blob_without_ambient_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SOR-221: a blob-carrying account with no mountable Secret still gets
    its credential at ``runner init`` — the registry blob rides the init
    env (the same lane the verify probe uses), so the sandbox restores the
    auth files even when ``secret_name`` is empty and no ambient
    ``SBX_ACCOUNT_CREDENTIAL`` exists."""
    monkeypatch.delenv("SBX_ACCOUNT_CREDENTIAL", raising=False)
    monkeypatch.delenv("SBX_ACCOUNT_ID", raising=False)
    registry = PersistentAccountRegistry(InMemoryAccountStore())
    registry.put(
        Account(
            id=ACCOUNT_ID,
            provider="codex",
            label="h",
            secret_name="",  # blob-only account — the reported gap
            created_at="2026-09-19T00:00:00+00:00",
        )
    )
    registry.put_credential_blob(ACCOUNT_ID, _blob("seeded"))
    backend = LocalProcessBackend()
    plane = ControlPlane(backend, InMemoryStore(), RUNNER)
    plane.credential_sync = CredentialSync(lambda: registry)
    session_id = plane.create_session(
        owner="sbx",
        title="t",
        model="gpt-5.6-luna",
        provider="codex",
        account_id=ACCOUNT_ID,
    )
    try:
        rec = plane.get(session_id)
        assert rec is not None and rec.status == "idle"
        restored = Path(rec.sandbox_root) / "home" / ".codex" / "auth.json"
        assert restored.is_file()
        assert "REDACTED-seeded" in restored.read_text(encoding="utf-8")
    finally:
        plane.close(session_id)


def test_mark_run_credential_pins_base_fp() -> None:
    tags = {TAG_CRED_BASE_FP: "abc"}
    CredentialSync.mark_run_credential(tags)
    assert tags[TAG_CRED_RUN_FP] == "abc"
    empty: dict[str, str] = {}
    CredentialSync.mark_run_credential(empty)
    assert TAG_CRED_RUN_FP not in empty


def test_writeback_commits_rotated_blob_and_refreshes_secret(cred_env: _CredEnv) -> None:
    rotated = cred_env.rotate()
    outcome = cred_env.writeback()

    assert outcome.code == "committed"
    assert outcome.fingerprint == blob_fingerprint(rotated)
    assert outcome.secret == "refreshed"
    assert cred_env.registry.get_credential_blob(ACCOUNT_ID) == rotated
    assert len(cred_env.writer.calls) == 1
    secret_name, env = cred_env.writer.calls[0]
    assert secret_name == MANAGED_SECRET
    assert set(env) == {"SBX_ACCOUNT_CREDENTIAL"}
    assert json.loads(env["SBX_ACCOUNT_CREDENTIAL"]) == rotated


def test_writeback_unchanged_leaves_store_and_secret(cred_env: _CredEnv) -> None:
    outcome = cred_env.writeback()
    assert outcome.code == "unchanged"
    assert cred_env.registry.get_credential_blob(ACCOUNT_ID) == _blob("old")
    assert cred_env.writer.calls == []


def test_writeback_stale_writer_dropped(cred_env: _CredEnv) -> None:
    # A concurrent writer (another session / manual refresh) rotated the
    # stored blob after this session was seeded.
    cred_env.registry.put_credential_blob(ACCOUNT_ID, _blob("concurrent"))
    base_fp = blob_fingerprint(_blob("old"))
    cred_env.rotate("stale-writer")

    outcome = cred_env.writeback(tags={"account_id": ACCOUNT_ID, TAG_CRED_BASE_FP: base_fp or ""})

    assert outcome.code == "stale"
    # The newer stored credential was not clobbered.
    assert cred_env.registry.get_credential_blob(ACCOUNT_ID) == _blob("concurrent")
    assert cred_env.writer.calls == []


def test_writeback_second_commit_after_base_refresh_is_stale(cred_env: _CredEnv) -> None:
    first = cred_env.rotate("first")
    assert cred_env.writeback().code == "committed"

    # Another write-back with the pre-commit base fingerprint must not land.
    cred_env.rotate("second")
    stale_fp = blob_fingerprint(_blob("old"))
    outcome = cred_env.writeback(tags={"account_id": ACCOUNT_ID, TAG_CRED_BASE_FP: stale_fp or ""})
    assert outcome.code == "stale"
    assert cred_env.registry.get_credential_blob(ACCOUNT_ID) == first


def test_writeback_rejects_schema_invalid_export(cred_env: _CredEnv) -> None:
    auth_path = cred_env.handle.root / "home" / ".codex" / "auth.json"
    auth_path.write_text("not-json\n", encoding="utf-8")

    outcome = cred_env.writeback()

    assert outcome.code == "invalid"
    assert cred_env.registry.get_credential_blob(ACCOUNT_ID) == _blob("old")
    assert cred_env.writer.calls == []


def test_writeback_heals_invalid_account_once(cred_env: _CredEnv) -> None:
    cred_env.registry.mark_status(ACCOUNT_ID, "invalid", last_error="auth_invalid")
    cred_env.rotate()

    outcome = cred_env.writeback()

    assert outcome.code == "committed"
    assert outcome.healed is True
    account = cred_env.registry.get(ACCOUNT_ID)
    assert account is not None
    assert account.status == "active"
    assert account.last_error is None


def test_writeback_skips_without_base_anchor(cred_env: _CredEnv) -> None:
    cred_env.rotate()
    outcome = cred_env.writeback(tags={"account_id": ACCOUNT_ID})
    assert outcome.code == "skipped:no_base"
    assert cred_env.registry.get_credential_blob(ACCOUNT_ID) == _blob("old")


def test_writeback_skips_auto_and_bad_accounts(cred_env: _CredEnv) -> None:
    for account_id in ("auto", "", "bad id"):
        outcome = cred_env.writeback(tags={"account_id": account_id, TAG_CRED_BASE_FP: "x"})
        assert outcome.code == "skipped:account"


def test_writeback_disabled_by_env(cred_env: _CredEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBX_CRED_WRITEBACK", "0")
    cred_env.rotate()
    assert cred_env.writeback().code == "skipped:disabled"
    assert cred_env.registry.get_credential_blob(ACCOUNT_ID) == _blob("old")


def test_writeback_no_registry_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _CredEnv(monkeypatch)
    try:
        sync = CredentialSync(lambda: None)
        env.rotate()
        outcome = sync.writeback(
            backend=env.backend,
            handle=env.handle,
            runner_cmd=RUNNER,
            tags=env.tags,
        )
        assert outcome.code == "skipped:no_registry"
    finally:
        env.close()


def test_writeback_without_secret_writer_stays_in_store(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _CredEnv(monkeypatch)
    try:
        sync = CredentialSync(lambda: env.registry)  # no secret writer
        rotated = env.rotate()
        outcome = sync.writeback(
            backend=env.backend, handle=env.handle, runner_cmd=RUNNER, tags=env.tags
        )
        assert outcome.code == "committed"
        assert outcome.secret == "skipped"
        assert env.registry.get_credential_blob(ACCOUNT_ID) == rotated
    finally:
        env.close()


def test_writeback_custom_secret_name_skips_secret_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _CredEnv(monkeypatch, secret_name="operator-managed-secret")
    try:
        env.rotate()
        outcome = env.writeback()
        assert outcome.code == "committed"
        assert outcome.secret == "skipped"
        # Custom-named Secrets are never recreated — deployment-managed names only.
        assert env.writer.calls == []
    finally:
        env.close()


def test_writeback_secret_refresh_failure_keeps_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _CredEnv(monkeypatch)
    try:

        class _Boom:
            def refresh(self, secret_name: str, env: dict[str, str]) -> None:
                raise RuntimeError("modal down")

        sync = CredentialSync(lambda: env.registry, secret_writer=_Boom())
        rotated = env.rotate()
        outcome = sync.writeback(
            backend=env.backend, handle=env.handle, runner_cmd=RUNNER, tags=env.tags
        )
        assert outcome.code == "committed"
        assert outcome.secret == "failed"
        assert env.registry.get_credential_blob(ACCOUNT_ID) == rotated
    finally:
        env.close()


def test_credential_rotated() -> None:
    registry = PersistentAccountRegistry(InMemoryAccountStore())
    registry.put(
        Account(id=ACCOUNT_ID, provider="codex", label="h", created_at="2026-09-19T00:00:00+00:00")
    )
    registry.put_credential_blob(ACCOUNT_ID, _blob("old"))
    fp = blob_fingerprint(_blob("old"))

    assert credential_rotated(registry, ACCOUNT_ID, fp) is False
    registry.put_credential_blob(ACCOUNT_ID, _blob("new"))
    assert credential_rotated(registry, ACCOUNT_ID, fp) is True
    assert credential_rotated(registry, ACCOUNT_ID, None) is False
    assert credential_rotated(registry, "auto", fp) is False
    assert credential_rotated(None, ACCOUNT_ID, fp) is False


# ------------------------------------------------------------ plane wiring


def _wait(pred, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _rotate_at(sandbox_root: str, marker: str = "new") -> dict[str, Any]:
    """Rewrite the session sandbox's restored credential file."""
    auth_path = Path(sandbox_root) / "home" / ".codex" / "auth.json"
    content = _blob(marker)["files"][".codex/auth.json"] + "\n"
    auth_path.write_text(content, encoding="utf-8")
    return {"provider": "codex", "files": {".codex/auth.json": content}}


def test_plane_seeds_base_fp_and_writes_back_after_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end over the real ControlPlane: provision seeds ``cred_base_fp``,
    a turn's finish commits a CLI-rotated blob back to the store."""
    from control.run_store import InMemoryRunStore, RunLedger

    env = _CredEnv(monkeypatch)
    try:
        store = InMemoryStore()
        plane = ControlPlane(
            env.backend,
            store,
            RUNNER,
            run_ledger=RunLedger(InMemoryRunStore()),
            clock=lambda: datetime.now(UTC),
        )
        plane.credential_sync = env.sync
        session_id = plane.create_session(
            owner="sbx",
            title="t",
            model="gpt-5.6-luna",
            provider="codex",
            account_id=ACCOUNT_ID,
            secret_name=MANAGED_SECRET,
        )
        rec = store.get(session_id)
        assert rec is not None
        seeded_fp = env.base_fp
        assert rec.sandbox_tags[TAG_CRED_BASE_FP] == seeded_fp

        # The CLI refreshes its token mid-session (the session's own sandbox).
        rotated = _rotate_at(rec.sandbox_root)
        plane.post_message(session_id, "hi")
        assert _wait(lambda: (store.get(session_id) or rec).status == "idle")
        # Write-back runs right after the turn settles — poll for the commit.
        assert _wait(lambda: env.registry.get_credential_blob(ACCOUNT_ID) == rotated)
        rec = store.get(session_id)
        assert rec is not None
        # The CAS comparand advanced to the committed blob; the run pin stays
        # at the fingerprint the run was actually seeded with.
        assert rec.sandbox_tags[TAG_CRED_BASE_FP] == blob_fingerprint(rotated)
        assert rec.sandbox_tags[TAG_CRED_RUN_FP] == seeded_fp
        assert env.writer.calls and env.writer.calls[0][0] == MANAGED_SECRET
        plane.close(session_id)
    finally:
        env.close()


def test_plane_close_writes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _CredEnv(monkeypatch)
    try:
        store = InMemoryStore()
        plane = ControlPlane(
            env.backend,
            store,
            RUNNER,
            clock=lambda: datetime.now(UTC),
        )
        plane.credential_sync = env.sync
        session_id = plane.create_session(
            owner="sbx",
            title="t",
            model="gpt-5.6-luna",
            provider="codex",
            account_id=ACCOUNT_ID,
        )
        rec = store.get(session_id)
        assert rec is not None
        rotated = _rotate_at(rec.sandbox_root)
        plane.close(session_id)
        assert env.registry.get_credential_blob(ACCOUNT_ID) == rotated
    finally:
        env.close()
