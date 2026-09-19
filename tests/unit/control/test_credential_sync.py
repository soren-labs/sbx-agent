"""SOR-147/WP-H1: runtime credential write-back — CAS commit, stale-writer
protection, managed Secret publish, one-shot auth_invalid self-heal.

Fixture credential material is fake (``REDACTED``); nothing here reads or
prints a real credential.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from control.accounts import (
    COMMIT_COMMITTED,
    COMMIT_STALE,
    COMMIT_UNCHANGED,
    InMemoryAccountStore,
    PersistentAccountRegistry,
    credential_blob_digest,
)
from control.api_v1.deps import RunFailureReporter
from control.api_v1.state import InMemoryAccountRegistry
from control.backend import LocalProcessBackend
from control.credential_sync import (
    COMMIT_NO_REGISTRY,
    COMMIT_REFUSED,
    CREDENTIAL_ENV,
    CredentialSync,
    parse_exported_blob,
)
from control.ports import Account
from control.service import ControlPlane
from control.store import InMemoryStore

AGY_FILE = ".gemini/antigravity-cli/antigravity-oauth-token"


def _blob(token: str) -> dict[str, Any]:
    return {
        "provider": "antigravity",
        "files": {AGY_FILE: json.dumps({"access_token": token})},
    }


def _account(id: str = "agy-1", **kw: Any) -> Account:
    kw.setdefault("provider", "antigravity")
    kw.setdefault("label", id)
    return Account(id=id, **kw)


def _registry() -> PersistentAccountRegistry:
    return PersistentAccountRegistry(InMemoryAccountStore())


class TestDigest:
    def test_stable_and_order_insensitive(self) -> None:
        a = {"provider": "x", "files": {"a": "1", "b": "2"}}
        b = {"files": {"b": "2", "a": "1"}, "provider": "x"}
        assert credential_blob_digest(a) == credential_blob_digest(b)
        assert credential_blob_digest(None) is None
        assert credential_blob_digest("nope") is None


class TestRegistryCas:
    def test_commit_when_base_matches(self) -> None:
        reg = _registry()
        reg.put(_account())
        old, new = _blob("REDACTED-0"), _blob("REDACTED-1")
        reg.put_credential_blob("agy-1", old)
        base = credential_blob_digest(old)
        assert reg.commit_credential_blob("agy-1", new, base_sha256=base) == COMMIT_COMMITTED
        assert reg.get_credential_blob("agy-1") == new

    def test_unchanged_is_noop(self) -> None:
        reg = _registry()
        reg.put(_account())
        blob = _blob("REDACTED-0")
        reg.put_credential_blob("agy-1", blob)
        assert (
            reg.commit_credential_blob("agy-1", _blob("REDACTED-0"), base_sha256="bogus")
            == COMMIT_UNCHANGED
        )

    def test_stale_writer_dropped(self) -> None:
        reg = _registry()
        reg.put(_account())
        v0, v1, v0_export = _blob("REDACTED-0"), _blob("REDACTED-1"), _blob("REDACTED-0")
        reg.put_credential_blob("agy-1", v0)
        base = credential_blob_digest(v0)
        # Another writer refreshed first.
        reg.put_credential_blob("agy-1", v1)
        # This session's stale export must not roll the store back.
        assert reg.commit_credential_blob("agy-1", v0_export, base_sha256=base) == COMMIT_STALE
        assert reg.get_credential_blob("agy-1") == v1

    def test_base_none_requires_empty_store(self) -> None:
        reg = _registry()
        reg.put(_account())
        assert (
            reg.commit_credential_blob("agy-1", _blob("REDACTED-1"), base_sha256=None)
            == COMMIT_COMMITTED
        )
        reg.put_credential_blob("agy-1", _blob("REDACTED-2"))
        assert (
            reg.commit_credential_blob("agy-1", _blob("REDACTED-3"), base_sha256=None)
            == COMMIT_STALE
        )

    def test_invalid_blob_and_id_rejected(self) -> None:
        reg = _registry()
        reg.put(_account())
        with pytest.raises(ValueError):
            reg.commit_credential_blob("agy-1", {"provider": "antigravity"}, base_sha256=None)
        with pytest.raises(ValueError):
            reg.commit_credential_blob("../x", _blob("REDACTED-1"), base_sha256=None)

    def test_provider_mismatch_rejected(self) -> None:
        reg = _registry()
        reg.put(_account())
        bad = {"provider": "codex", "files": {".codex/auth.json": "{}"}}
        with pytest.raises(ValueError):
            reg.commit_credential_blob("agy-1", bad, base_sha256=None)

    def test_in_memory_fallback_registry(self) -> None:
        reg = InMemoryAccountRegistry()
        reg.put(_account())
        old, new = _blob("REDACTED-0"), _blob("REDACTED-1")
        reg.put_credential_blob("agy-1", old)
        assert (
            reg.commit_credential_blob("agy-1", new, base_sha256=credential_blob_digest(old))
            == COMMIT_COMMITTED
        )
        assert (
            reg.commit_credential_blob("agy-1", _blob("REDACTED-2"), base_sha256=None)
            == COMMIT_STALE
        )


class TestCredentialSyncCommit:
    def _sync(
        self, registry: Any = None, **kw: Any
    ) -> tuple[CredentialSync, list[tuple[str, dict[str, str]]]]:
        writes: list[tuple[str, dict[str, str]]] = []
        writer = kw.pop("secret_writer", lambda name, env: writes.append((name, env)))
        return CredentialSync(registry, secret_writer=writer, **kw), writes

    def test_commit_publishes_managed_secret(self) -> None:
        reg = _registry()
        reg.put(_account(secret_name="sbx-acct-agy-1"))
        old = _blob("REDACTED-0")
        reg.put_credential_blob("agy-1", old)
        sync, writes = self._sync(reg)
        new = _blob("REDACTED-1")
        outcome = sync.commit("agy-1", new, base_sha256=credential_blob_digest(old))
        assert outcome == COMMIT_COMMITTED
        assert reg.get_credential_blob("agy-1") == new
        assert [name for name, _ in writes] == ["sbx-acct-agy-1"]
        env = writes[0][1]
        assert set(env) == {CREDENTIAL_ENV}
        assert json.loads(env[CREDENTIAL_ENV]) == new

    def test_custom_secret_name_never_overwritten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = _registry()
        reg.put(_account(secret_name="team-managed-secret"))
        old = _blob("REDACTED-0")
        reg.put_credential_blob("agy-1", old)
        sync, writes = self._sync(reg)
        outcome = sync.commit("agy-1", _blob("REDACTED-1"), base_sha256=credential_blob_digest(old))
        assert outcome == COMMIT_COMMITTED
        assert writes == []  # registry updated; external Secret untouched

    def test_no_writer_still_commits(self) -> None:
        reg = _registry()
        reg.put(_account(secret_name="sbx-acct-agy-1"))
        old = _blob("REDACTED-0")
        reg.put_credential_blob("agy-1", old)
        sync = CredentialSync(reg, secret_writer=None)
        assert (
            sync.commit("agy-1", _blob("REDACTED-1"), base_sha256=credential_blob_digest(old))
            == COMMIT_COMMITTED
        )

    def test_publish_failure_keeps_registry_truth(self) -> None:
        reg = _registry()
        reg.put(_account(secret_name="sbx-acct-agy-1"))
        old = _blob("REDACTED-0")
        reg.put_credential_blob("agy-1", old)
        sync = CredentialSync(
            reg, secret_writer=lambda *_a: (_ for _ in ()).throw(RuntimeError("modal down"))
        )
        assert (
            sync.commit("agy-1", _blob("REDACTED-1"), base_sha256=credential_blob_digest(old))
            == COMMIT_COMMITTED
        )
        assert reg.get_credential_blob("agy-1") == _blob("REDACTED-1")

    def test_refusal_paths(self) -> None:
        reg = _registry()
        reg.put(_account())
        sync, _ = self._sync(reg)
        assert sync.commit("agy-1", {"files": {}}, base_sha256=None) == COMMIT_REFUSED
        assert sync.commit("agy-1", "not-a-dict", base_sha256=None) == COMMIT_REFUSED
        assert sync.commit("../escape", _blob("REDACTED-1"), base_sha256=None) == COMMIT_REFUSED
        assert sync.commit("missing-acct", _blob("REDACTED-1"), base_sha256=None) == COMMIT_REFUSED
        # Undeclared credential path → refused by descriptor validation.
        bad = {"provider": "antigravity", "files": {".ssh/id_rsa": "REDACTED"}}
        assert sync.commit("agy-1", bad, base_sha256=None) == COMMIT_REFUSED

    def test_no_registry(self) -> None:
        sync, _ = self._sync(lambda: None)
        assert sync.commit("agy-1", _blob("REDACTED-1"), base_sha256=None) == COMMIT_NO_REGISTRY

    def test_stale_publish_never_reaches_secret(self) -> None:
        reg = _registry()
        reg.put(_account(secret_name="sbx-acct-agy-1"))
        reg.put_credential_blob("agy-1", _blob("REDACTED-0"))
        sync, writes = self._sync(reg)
        reg.put_credential_blob("agy-1", _blob("REDACTED-1"))
        base = credential_blob_digest(_blob("REDACTED-0"))
        assert sync.commit("agy-1", _blob("REDACTED-0"), base_sha256=base) == COMMIT_STALE
        assert writes == []


class TestSelfHeal:
    def test_commit_heals_auth_invalid_parking(self) -> None:
        reg = _registry()
        reg.put(_account())
        reg.mark_status("agy-1", "invalid", last_error="auth_invalid")
        old = _blob("REDACTED-0")
        reg.put_credential_blob("agy-1", old)
        sync = CredentialSync(reg)
        assert (
            sync.commit("agy-1", _blob("REDACTED-1"), base_sha256=credential_blob_digest(old))
            == COMMIT_COMMITTED
        )
        healed = reg.get("agy-1")
        assert healed is not None and healed.status == "active" and healed.last_error is None

    def test_commit_does_not_heal_other_invalid_parks(self) -> None:
        reg = _registry()
        reg.put(_account())
        reg.mark_status("agy-1", "invalid", last_error="quota_exhausted")
        old = _blob("REDACTED-0")
        reg.put_credential_blob("agy-1", old)
        sync = CredentialSync(reg)
        sync.commit("agy-1", _blob("REDACTED-1"), base_sha256=credential_blob_digest(old))
        assert reg.get("agy-1").status == "invalid"

    def test_skip_auth_invalid_report_when_superseded(self) -> None:
        reg = _registry()
        reg.put(_account())
        reg.mark_status("agy-1", "invalid", last_error="auth_invalid")
        v0, v1 = _blob("REDACTED-0"), _blob("REDACTED-1")
        reg.put_credential_blob("agy-1", v1)
        sync = CredentialSync(reg)
        # Session mounted v0; store already carries v1 → superseded.
        assert sync.skip_auth_invalid_report("agy-1", credential_blob_digest(v0))
        assert reg.get("agy-1").status == "active"  # unparked by the heal

    def test_no_skip_when_credential_unchanged(self) -> None:
        reg = _registry()
        reg.put(_account())
        v0 = _blob("REDACTED-0")
        reg.put_credential_blob("agy-1", v0)
        sync = CredentialSync(reg)
        assert not sync.skip_auth_invalid_report("agy-1", credential_blob_digest(v0))


class _FakeScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def report_failure(self, account_id: str, kind: str, **kw: Any) -> None:
        self.calls.append((account_id, kind))


class _FakePlane:
    def __init__(self, sync: CredentialSync, base: tuple[bool, str | None]) -> None:
        self.credential_sync = sync
        self._base = base

    def credential_base(self, session_id: str) -> tuple[bool, str | None]:
        return self._base


class TestReporterSelfHeal:
    _ERROR = {"code": "auth_invalid", "message": "token rejected"}

    def test_superseded_auth_invalid_is_dropped_and_healed(self) -> None:
        reg = _registry()
        reg.put(_account())
        reg.mark_status("agy-1", "invalid", last_error="auth_invalid")
        v0, v1 = _blob("REDACTED-0"), _blob("REDACTED-1")
        reg.put_credential_blob("agy-1", v1)
        plane = _FakePlane(CredentialSync(reg), (True, credential_blob_digest(v0)))
        scheduler = _FakeScheduler()
        RunFailureReporter().report(
            scheduler=scheduler,
            agent_id="agent-1",
            n=1,
            account_id="agy-1",
            status="ERROR",
            error=dict(self._ERROR),
            plane=plane,
        )
        assert scheduler.calls == []
        assert reg.get("agy-1").status == "active"

    def test_unsuperseded_auth_invalid_still_reports(self) -> None:
        reg = _registry()
        reg.put(_account())
        v0 = _blob("REDACTED-0")
        reg.put_credential_blob("agy-1", v0)
        plane = _FakePlane(CredentialSync(reg), (True, credential_blob_digest(v0)))
        scheduler = _FakeScheduler()
        RunFailureReporter().report(
            scheduler=scheduler,
            agent_id="agent-1",
            n=1,
            account_id="agy-1",
            status="ERROR",
            error=dict(self._ERROR),
            plane=plane,
        )
        assert scheduler.calls == [("agy-1", "auth_invalid")]

    def test_uncaptured_base_keeps_reporting(self) -> None:
        reg = _registry()
        reg.put(_account())
        reg.put_credential_blob("agy-1", _blob("REDACTED-1"))
        plane = _FakePlane(CredentialSync(reg), (False, None))
        scheduler = _FakeScheduler()
        RunFailureReporter().report(
            scheduler=scheduler,
            agent_id="agent-1",
            n=1,
            account_id="agy-1",
            status="ERROR",
            error=dict(self._ERROR),
            plane=plane,
        )
        assert scheduler.calls == [("agy-1", "auth_invalid")]


class TestParseExportedBlob:
    def test_last_line_wins(self) -> None:
        blob = _blob("REDACTED-1")
        lines = ['{"type": "sbx.event"}', "", json.dumps(blob)]
        assert parse_exported_blob(lines) == blob

    def test_empty_and_garbage(self) -> None:
        assert parse_exported_blob([]) is None
        assert parse_exported_blob(["", "  "]) is None
        assert parse_exported_blob(["not json"]) is None
        assert parse_exported_blob(['"just a string"']) is None


_STUB_RUNNER = Path(__file__).resolve().parents[3] / "tests" / "fakes" / "stub_runner.py"


def _plane() -> ControlPlane:
    backend = LocalProcessBackend()
    return ControlPlane(
        backend,
        InMemoryStore(),
        [sys.executable, str(_STUB_RUNNER)],
    )


class TestPlaneWriteback:
    def _session_env(self, monkeypatch: pytest.MonkeyPatch, account_id: str, blob: dict) -> None:
        # sandbox_env forwards the ambient account blob only when the scoped
        # account id matches the sandbox tag (SOR-80 rules unchanged).
        monkeypatch.setenv("SBX_ACCOUNT_ID", account_id)
        monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", json.dumps(blob))

    def test_writeback_commits_refreshed_files(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = _registry()
        reg.put(_account(secret_name="sbx-acct-agy-1"))
        old = _blob("REDACTED-0")
        reg.put_credential_blob("agy-1", old)
        writes: list[tuple[str, dict[str, str]]] = []
        plane = _plane()
        plane.credential_sync = CredentialSync(
            reg, secret_writer=lambda name, env: writes.append((name, env))
        )
        self._session_env(monkeypatch, "agy-1", old)
        session_id = plane.create_session(
            owner="o", title="t", model="m", provider="antigravity", account_id="agy-1"
        )
        rec = plane.get(session_id)
        assert rec is not None and rec.status == "idle"
        # The provisioned session pinned the v0 digest.
        captured, base = plane.credential_base(session_id)
        assert captured and base == credential_blob_digest(old)
        # The CLI refreshed its own auth file inside the sandbox.
        auth_file = Path(rec.sandbox_root) / "home" / AGY_FILE
        assert auth_file.is_file()
        refreshed = json.dumps({"access_token": "REDACTED-9"})
        auth_file.write_text(refreshed, encoding="utf-8")
        plane._writeback_credentials(session_id)
        stored = reg.get_credential_blob("agy-1")
        assert stored is not None and stored["files"][AGY_FILE] == refreshed
        assert writes and writes[-1][0] == "sbx-acct-agy-1"
        plane.close(session_id)

    def test_stale_writeback_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = _registry()
        reg.put(_account())
        v0, v1 = _blob("REDACTED-0"), _blob("REDACTED-1")
        reg.put_credential_blob("agy-1", v0)
        plane = _plane()
        plane.credential_sync = CredentialSync(reg)
        self._session_env(monkeypatch, "agy-1", v0)
        session_id = plane.create_session(
            owner="o", title="t", model="m", provider="antigravity", account_id="agy-1"
        )
        rec = plane.get(session_id)
        assert rec is not None
        # Another writer refreshed the credential mid-session; the exec env
        # now resolves v1 while the sandbox files still hold v0 — exactly the
        # Secret-remount hazard. The v0 export must not roll the store back.
        reg.put_credential_blob("agy-1", v1)
        monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", json.dumps(v1))
        plane._writeback_credentials(session_id)
        assert reg.get_credential_blob("agy-1") == v1
        plane.close(session_id)

    def test_unchanged_files_no_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = _registry()
        reg.put(_account())
        v0 = _blob("REDACTED-0")
        reg.put_credential_blob("agy-1", v0)
        plane = _plane()
        plane.credential_sync = CredentialSync(reg)
        self._session_env(monkeypatch, "agy-1", v0)
        session_id = plane.create_session(
            owner="o", title="t", model="m", provider="antigravity", account_id="agy-1"
        )
        plane._writeback_credentials(session_id)
        assert reg.get_credential_blob("agy-1") == v0
        plane.close(session_id)

    def test_no_sync_lane_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plane = _plane()
        self._session_env(monkeypatch, "agy-1", _blob("REDACTED-0"))
        session_id = plane.create_session(
            owner="o", title="t", model="m", provider="antigravity", account_id="agy-1"
        )
        captured, _ = plane.credential_base(session_id)
        assert not captured
        plane._writeback_credentials(session_id)  # no-op, no raise
        plane.close(session_id)
