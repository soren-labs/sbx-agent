"""SOR-213/SOR-216: canonical provider auth — adapter, AuthService, and the
verified-only account lifecycle.

Deterministic: scripted probes, in-memory/file stores, tmp HOMEs. No real
credentials, no vendor CLIs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from control.accounts import (
    FileAccountStore,
    InMemoryAccountStore,
    PersistentAccountRegistry,
)
from control.credlifecycle import CredentialLifecycleService
from control.onboarding import (
    OnboardingError,
    OnboardingService,
    ProbeResult,
)
from control.ports import Account
from control.provider_auth import (
    AUTH_SESSION_STATES,
    AuthService,
    adapter_for,
    auth_state_for,
    host_cli_env,
    provider_login_argv,
    supported_auth_providers,
)
from control.scheduler import AccountScheduler

SECRET = "SECRET_TOKEN_VALUE_providerauth_71f2"
GROK_AUTH_REL = ".grok/auth.json"
CODEX_AUTH_REL = ".codex/auth.json"


class ScriptedProbe:
    """Deterministic CredentialProbe; authoritative unless declared else."""

    def __init__(self, status: str, *, authoritative: bool = True) -> None:
        self.result = ProbeResult(status, "scripted")
        self.authoritative = authoritative
        self.calls = 0

    def probe(self, account: Account, blob: dict | None) -> ProbeResult:
        self.calls += 1
        return self.result


def _write(path: Path, content: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def _service(
    store: Any | None = None, probe: Any | None = None, home: Path | None = None
) -> AuthService:
    registry = PersistentAccountRegistry(store or InMemoryAccountStore())
    onboarding = OnboardingService(registry, probe)
    return AuthService(registry, onboarding=onboarding, home=home, env={})


def _creds(home: Path, token: str = "x") -> Path:
    return _write(home / GROK_AUTH_REL, f'{{"token": "{token}"}}')


class TestAdapterAndArgv:
    def test_official_login_argv(self) -> None:
        assert provider_login_argv("codex", env={}) == ["codex", "login"]
        assert provider_login_argv("devin", env={}) == ["devin"]
        assert provider_login_argv("antigravity", env={}) == ["agy"]
        assert provider_login_argv("grok", env={}) == ["grok"]
        assert provider_login_argv("opencode", env={}) == ["opencode", "auth", "login"]

    def test_no_login_flow_for_unsupported(self) -> None:
        assert provider_login_argv("claude", env={}) is None
        assert provider_login_argv("bogus", env={}) is None

    def test_login_argv_bin_override_splits_tokens(self) -> None:
        argv = provider_login_argv("grok", env={"GROK_BIN": "/opt/grok --fast"})
        assert argv == ["/opt/grok", "--fast"]

    def test_login_argv_py_bin_reexecs_with_interpreter(self) -> None:
        argv = provider_login_argv("codex", env={"CODEX_BIN": "/fakes/fake_codex.py"})
        assert argv == [sys.executable, "/fakes/fake_codex.py", "login"]

    def test_supported_auth_providers(self) -> None:
        assert supported_auth_providers() == sorted(
            ("codex", "devin", "antigravity", "grok", "opencode")
        )

    def test_adapter_exposes_declared_files(self) -> None:
        adapter = adapter_for("grok")
        assert adapter.provider == "grok"
        assert adapter.credential_files == (GROK_AUTH_REL,)
        assert adapter.auth_check_argv(env={}) == ["grok", "models"]

    def test_capture_only_reads_declared_files(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _creds(home, token=SECRET)
        # A stray file next to the credential is never captured.
        _write(home / ".grok" / "other.txt", "unrelated")
        blob = adapter_for("grok").capture(home)
        assert blob == {
            "provider": "grok",
            "files": {GROK_AUTH_REL: f'{{"token": "{SECRET}"}}'},
        }

    def test_capture_missing_declared_files(self, tmp_path: Path) -> None:
        (tmp_path / "empty-home").mkdir()
        with pytest.raises(OnboardingError) as exc:
            adapter_for("grok").capture(tmp_path / "empty-home")
        assert exc.value.code == "missing_credential_file"


class TestHostCliEnv:
    def test_scrubs_ambient_credentials(self, tmp_path: Path) -> None:
        parent = {
            "HOME": "/real/home",
            "PATH": "/usr/bin",
            "GROK_TOKEN": SECRET,
            "OPENAI_API_KEY": SECRET,
            "MODAL_TOKEN_ID": SECRET,
        }
        env = host_cli_env("grok", tmp_path / "home", parent)
        assert env["HOME"] == str(tmp_path / "home")
        assert env["PATH"] == "/usr/bin"
        assert SECRET not in json.dumps(env)
        assert "GROK_TOKEN" not in env
        assert "MODAL_TOKEN_ID" not in env

    def test_xdg_pinned_for_devin_and_opencode(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        env = host_cli_env("devin", home, {})
        assert env["XDG_DATA_HOME"] == str(home / ".local" / "share")
        assert host_cli_env("grok", home, {}).get("XDG_DATA_HOME") is None


class TestAuthStateFor:
    """The canonical auth-session state vocabulary (AUTH_SESSION_STATES)."""

    def _account(self, status: str, **kw: Any) -> Account:
        return Account(id="a1", provider="grok", label="a1", status=status, **kw)

    def test_disabled(self) -> None:
        a = self._account("disabled")
        rec = {"state": "healthy", "verified_at": 1}
        assert auth_state_for(a, rec, has_credential=True) == "disabled"

    def test_reauth_required_lifecycle(self) -> None:
        a = self._account("invalid")
        assert auth_state_for(a, {"state": "reauth_required"}, True) == "reauth_required"
        assert auth_state_for(a, {"state": "revoked"}, True) == "reauth_required"
        assert auth_state_for(a, {"state": "healthy"}, True) == "reauth_required"

    def test_unhealthy_cooling(self) -> None:
        a = self._account("cooling")
        assert auth_state_for(a, {"state": "healthy"}, True) == "unhealthy"

    def test_verified_needs_active_and_evidence(self) -> None:
        a = self._account("active")
        assert auth_state_for(a, {"verified_at": 123}, True) == "verified"
        # verified evidence without active status is not scheduler-eligible.
        b = self._account("unverified")
        assert auth_state_for(b, {"verified_at": 123}, True) == "materialized"

    def test_materialized_vs_unauthenticated(self) -> None:
        a = self._account("unverified")
        assert auth_state_for(a, {}, True) == "materialized"
        assert auth_state_for(a, None, True) == "materialized"
        b = self._account("unverified", secret_name="sbx-acct-x")
        assert auth_state_for(b, None, False) == "materialized"
        c = self._account("unverified")
        assert auth_state_for(c, None, False) == "unauthenticated"

    def test_state_vocabulary_is_canonical(self) -> None:
        assert AUTH_SESSION_STATES == (
            "unauthenticated",
            "authenticating",
            "authenticated",
            "materialized",
            "verified",
            "reauth_required",
            "unhealthy",
            "disabled",
        )


class TestImportVerifyEligibility:
    """SOR-216: scheduler eligibility requires materialization + verify."""

    def test_import_lands_unverified_not_schedulable(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _creds(home)
        svc = _service(home=home)
        outcome = svc.import_existing("grok", verify=False)
        session = outcome["session"]
        assert session["status"] == "unverified"
        assert session["auth_state"] == "materialized"
        assert outcome["verified"] is False
        scheduler = AccountScheduler(svc.registry)
        assert scheduler.decide(provider="grok").error == "provider_exhausted"

    def test_verify_promotes_to_active(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _creds(home)
        svc = _service(probe=ScriptedProbe("ok"), home=home)
        outcome = svc.import_existing("grok")
        assert outcome["verified"] is True
        session = outcome["session"]
        assert session["status"] == "active"
        assert session["auth_state"] == "verified"
        assert session["verified_at"] is not None
        scheduler = AccountScheduler(svc.registry)
        decision = scheduler.decide(provider="grok")
        assert decision.account is not None
        assert decision.account.id == outcome["account_id"]

    def test_nonauthoritative_ok_never_promotes(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _creds(home)
        svc = _service(probe=ScriptedProbe("ok", authoritative=False), home=home)
        outcome = svc.import_existing("grok")
        assert outcome["verified"] is False
        assert outcome["session"]["status"] == "unverified"
        assert svc.registry.get(outcome["account_id"]).status == "unverified"  # type: ignore[union-attr]

    def test_auth_invalid_exits_scheduling_immediately(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _creds(home)
        svc = _service(probe=ScriptedProbe("ok"), home=home)
        outcome = svc.import_existing("grok")
        account_id = outcome["account_id"]
        assert svc.session(account_id).schedulable is True
        # The provider rejects the grant (e.g. mid-run 401).
        lifecycle = CredentialLifecycleService(svc.registry)
        lifecycle.on_auth_invalid(account_id)
        session = svc.session(account_id)
        assert session.auth_state == "reauth_required"
        assert session.schedulable is False
        assert AccountScheduler(svc.registry).decide(provider="grok").error == "provider_exhausted"

    def test_relink_restores_eligibility(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        creds = _creds(home, token="old")
        svc = _service(probe=ScriptedProbe("ok"), home=home)
        outcome = svc.import_existing("grok")
        account_id = outcome["account_id"]
        CredentialLifecycleService(svc.registry).on_auth_invalid(account_id)
        svc.registry.mark_status(account_id, "invalid")
        assert svc.session(account_id).auth_state == "reauth_required"
        # A fresh grant lands at the same path; relink re-captures + verifies.
        _write(creds, '{"token": "rotated"}')
        relinked = svc.relink(account_id)
        assert relinked["verified"] is True
        assert relinked["session"]["auth_state"] == "verified"
        assert relinked["session"]["schedulable"] is True
        assert AccountScheduler(svc.registry).decide(provider="grok").account is not None

    def test_refresh_fp_change_demotes_active(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _creds(home, token="v1")
        svc = _service(probe=ScriptedProbe("ok"), home=home)
        outcome = svc.import_existing("grok")
        account_id = outcome["account_id"]
        assert svc.session(account_id).auth_state == "verified"
        # A different grant clears verified_at — unproven material never
        # stays scheduler-eligible.
        _write(home / GROK_AUTH_REL, '{"token": "rotated"}')
        svc.relink(account_id, verify=False)
        session = svc.session(account_id)
        assert session.status == "unverified"
        assert session.auth_state == "materialized"
        assert session.schedulable is False
        # Re-verify restores eligibility.
        again = svc.verify(account_id)
        assert again["verified"] is True
        assert svc.session(account_id).schedulable is True

    def test_logout_drops_all_credential_material(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        creds = _creds(home, token=SECRET)
        deleted: list[str] = []
        svc = _service(probe=ScriptedProbe("ok"), home=home)
        outcome = svc.import_existing("grok")
        account_id = outcome["account_id"]
        assert svc.session(account_id).auth_state == "verified"

        result = svc.logout(
            account_id,
            remove_local=True,
            secret_deleter=lambda name: deleted.append(name) or True,
        )
        assert deleted == [f"sbx-acct-{account_id}"]
        assert f"~/{creds.name}" in result["removed"]
        assert not creds.exists()
        assert svc.registry.get_credential_blob(account_id) is None
        session = svc.session(account_id)
        assert session.status == "unverified"
        assert session.auth_state == "unauthenticated"
        assert session.has_credential is False

    def test_logout_never_deletes_custom_secret(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _creds(home)
        svc = _service(probe=ScriptedProbe("ok"), home=home)
        account = svc.import_existing("grok")["account_id"]
        rec = svc.registry.get(account)
        assert rec is not None
        svc.registry.put(
            Account(
                id=rec.id,
                provider=rec.provider,
                label=rec.label,
                status=rec.status,
                secret_name="external-byo",
            )
        )
        deleted: list[str] = []
        svc.logout(account, secret_deleter=lambda name: deleted.append(name) or True)
        assert deleted == []

    def test_multi_account_preserved(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _creds(home)
        svc = _service(probe=ScriptedProbe("ok"), home=home)
        first = svc.import_existing("grok", label="g1")
        second = svc.import_existing("grok", label="g2", verify=False)
        sessions = {s["account_id"]: s for s in svc.status("grok")}
        # status() payloads are dicts; sessions() gives AuthSession objects.
        assert sessions[first["account_id"]]["auth_state"] == "verified"
        assert sessions[second["account_id"]]["auth_state"] == "materialized"
        # auto only ever picks the verified member.
        decision = AccountScheduler(svc.registry).decide(provider="grok")
        assert decision.account is not None
        assert decision.account.id == first["account_id"]
        # ... and the named unverified member is refused.
        decision = AccountScheduler(svc.registry).decide(
            provider="grok", account=second["account_id"]
        )
        assert decision.error == "account_unavailable"


class TestIsolationAndRedaction:
    """Local-vs-cloud credential isolation + secret redaction."""

    def test_store_lanes_keep_material_out_of_records(self, tmp_path: Path) -> None:
        store = FileAccountStore(tmp_path / "store")
        home = tmp_path / "home"
        _creds(home, token=SECRET)
        svc = _service(store=store, home=home)
        outcome = svc.import_existing("grok", verify=False)
        account_id = outcome["account_id"]
        record = json.loads((tmp_path / "store" / "accounts" / f"{account_id}.json").read_text())
        assert SECRET not in json.dumps(record)
        assert "files" not in record
        blob_file = tmp_path / "store" / "credentials" / f"{account_id}.json"
        assert SECRET in blob_file.read_text()

    def test_status_payload_never_carries_credential(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _creds(home, token=SECRET)
        svc = _service(probe=ScriptedProbe("ok"), home=home)
        svc.import_existing("grok")
        payload = json.dumps(svc.status())
        assert SECRET not in payload
        for session in svc.status("grok"):
            assert "files" not in session
            assert "credential" not in session

    def test_login_outcome_never_carries_credential(self, tmp_path: Path) -> None:
        home = tmp_path / "home"

        def fake_runner(argv: list[str], env: dict[str, str]) -> int:
            _creds(home, token=SECRET)
            return 0

        svc = _service(probe=ScriptedProbe("ok"), home=home)
        outcome = svc.login("grok", runner=fake_runner)
        assert outcome["verified"] is True
        assert SECRET not in json.dumps(outcome)

    def test_login_failed_runner_creates_nothing(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        svc = _service(home=home)
        with pytest.raises(OnboardingError) as exc:
            svc.login("grok", runner=lambda argv, env: 1)
        assert exc.value.code == "login_failed"
        assert svc.registry.list() == []

    def test_login_missing_cli_is_cli_missing(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        svc = _service(home=home)

        def missing(argv: list[str], env: dict[str, str]) -> int:
            raise OnboardingError("cli_missing", "no binary")

        with pytest.raises(OnboardingError) as exc:
            svc.login("grok", runner=missing)
        assert exc.value.code == "cli_missing"

    def test_login_existing_account_relinks(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _creds(home, token="v1")
        svc = _service(probe=ScriptedProbe("ok"), home=home)
        outcome = svc.import_existing("grok", verify=False)
        account_id = outcome["account_id"]

        def relogin(argv: list[str], env: dict[str, str]) -> int:
            _write(home / GROK_AUTH_REL, '{"token": "v2"}')
            return 0

        again = svc.login("grok", account_id=account_id, runner=relogin)
        assert again["login"] is True
        assert again["created"] is False
        assert svc.registry.get_credential_blob(account_id)["files"] == {
            GROK_AUTH_REL: '{"token": "v2"}'
        }
        assert svc.session(account_id).auth_state == "verified"
