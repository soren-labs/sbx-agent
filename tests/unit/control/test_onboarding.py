"""SOR-99: provider credential onboarding — descriptors, validation, verify seam,
refresh/export atomicity, disable/remove safety. Deterministic: fake sources,
scripted probes, stub_runner sandboxes. No real credentials."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from control.accounts import (
    FileAccountStore,
    InMemoryAccountStore,
    PersistentAccountRegistry,
)
from control.backend import LocalProcessBackend
from control.onboarding import (
    PROVIDER_DESCRIPTORS,
    OnboardingError,
    OnboardingService,
    ProbeResult,
    SandboxVerifyProbe,
    StaticCredentialProbe,
    collect_credential_blob,
    descriptor_for,
    validate_credential_blob,
)
from control.onboarding import main as onboarding_main
from control.ports import Account
from control.scheduler import AccountScheduler

SECRET = "SECRET_TOKEN_VALUE_9f3e"

GROK_AUTH_REL = ".grok/auth.json"
CODEX_AUTH_REL = ".codex/auth.json"
DEVIN_TOML_REL = ".local/share/devin/credentials.toml"
AGY_TOKEN_REL = ".gemini/antigravity-cli/antigravity-oauth-token"
OPENCODE_AUTH_REL = ".local/share/opencode/auth.json"
CLAUDE_CRED_REL = ".claude/.credentials.json"


def _write(path: Path, content: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def _service(store: Any | None = None, probe: Any | None = None) -> OnboardingService:
    # FileAccountStore over tmp dirs covers persistence; memory covers speed.
    return OnboardingService(
        PersistentAccountRegistry(store if store is not None else InMemoryAccountStore()),
        probe,
    )


def _registry(service: OnboardingService):
    return service.registry


class TestDescriptors:
    def test_support_tiers_match_the_release_matrix(self) -> None:
        """Descriptors must not claim a tier above the docs/providers.md
        evidence matrix — ``providers``/``status`` output is user-facing."""
        tiers = {d.provider: d.support for d in PROVIDER_DESCRIPTORS}
        assert tiers == {
            "codex": "stable",
            "devin": "experimental",
            "antigravity": "experimental",
            "grok": "experimental",
            "opencode": "preview",
            "claude": "unsupported",
        }

    def test_every_release_provider_imports_without_flag(self) -> None:
        # The five released providers are not behind --experimental; only
        # claude (unregistered adapter, unsupported in 0.1) is gated.
        free = {d.provider for d in PROVIDER_DESCRIPTORS if not d.experimental}
        assert free == {"codex", "devin", "antigravity", "grok", "opencode"}

    def test_claude_is_unsupported_and_gated(self) -> None:
        desc = descriptor_for("claude")
        assert desc.support == "unsupported"
        assert desc.experimental is True
        assert desc.credential_files == (CLAUDE_CRED_REL,)

    def test_credential_files_match_contract(self) -> None:
        files = {d.provider: d.credential_files for d in PROVIDER_DESCRIPTORS}
        assert files["codex"] == (CODEX_AUTH_REL,)
        assert files["devin"] == (DEVIN_TOML_REL,)
        assert files["antigravity"] == (AGY_TOKEN_REL,)
        assert files["grok"] == (GROK_AUTH_REL,)
        assert files["opencode"] == (OPENCODE_AUTH_REL,)

    def test_unknown_provider(self) -> None:
        with pytest.raises(OnboardingError) as exc:
            descriptor_for("not-a-provider")
        assert exc.value.code == "unknown_provider"


class TestAdd:
    def test_file_import_maps_to_declared_relpath(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", f'{{"token": "{SECRET}"}}')
        svc = _service()
        account = svc.add("grok", src, label="grok-01", slots=2)
        assert account.provider == "grok"
        assert account.secret_name == f"sbx-acct-{account.id}"
        # Registry record is metadata only — credential lives in the blob slot.
        blob = _registry(svc).get_credential_blob(account.id)
        assert blob == {"provider": "grok", "files": {GROK_AUTH_REL: f'{{"token": "{SECRET}"}}'}}
        record = svc.status(account.id)
        assert SECRET not in json.dumps(record)

    def test_directory_import(self, tmp_path: Path) -> None:
        src_dir = tmp_path / "dotcodex"
        _write(src_dir / "auth.json", f'{{"token": "{SECRET}"}}')
        svc = _service()
        account = svc.add("codex", src_dir)
        blob = _registry(svc).get_credential_blob(account.id)
        assert blob["files"] == {CODEX_AUTH_REL: f'{{"token": "{SECRET}"}}'}

    def test_directory_import_nested_relpath(self, tmp_path: Path) -> None:
        src_dir = tmp_path / "grokhome"
        _write(src_dir / GROK_AUTH_REL, f'{{"token": "{SECRET}"}}')
        svc = _service()
        account = svc.add("grok", src_dir)
        blob = _registry(svc).get_credential_blob(account.id)
        assert blob["files"] == {GROK_AUTH_REL: f'{{"token": "{SECRET}"}}'}

    def test_blob_json_file_import(self, tmp_path: Path) -> None:
        blob = {"provider": "devin", "files": {DEVIN_TOML_REL: f'windsurf_api_key = "{SECRET}"\n'}}
        src = _write(tmp_path / "blob.json", json.dumps(blob))
        svc = _service()
        account = svc.add("devin", src)
        assert _registry(svc).get_credential_blob(account.id) == blob

    def test_blob_stdin_import(self) -> None:
        blob = {"provider": "grok", "files": {GROK_AUTH_REL: '{"token": "x"}'}}
        svc = _service()
        account = svc.add("grok", "-", stdin_text=json.dumps(blob))
        assert _registry(svc).get_credential_blob(account.id) == blob

    def test_experimental_requires_flag(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "creds.json", '{"token": "x"}')
        svc = _service()
        with pytest.raises(OnboardingError) as exc:
            svc.add("claude", src)
        assert exc.value.code == "experimental_provider"
        account = svc.add("claude", src, experimental_ok=True)
        assert account.provider == "claude"

    def test_account_exists(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", '{"token": "x"}')
        svc = _service()
        svc.add("grok", src, account_id="g1")
        with pytest.raises(OnboardingError) as exc:
            svc.add("grok", src, account_id="g1")
        assert exc.value.code == "account_exists"

    def test_models_override_and_defaults(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", '{"token": "x"}')
        svc = _service()
        a1 = svc.add("grok", src)
        assert a1.models == ("grok-4.6",)
        a2 = svc.add("codex", src, models=("m1", "m2"))
        assert a2.models == ("m1", "m2")


class TestImportValidation:
    def test_provider_mismatch_blob(self, tmp_path: Path) -> None:
        blob = {"provider": "codex", "files": {CODEX_AUTH_REL: "{}"}}
        src = _write(tmp_path / "blob.json", json.dumps(blob))
        with pytest.raises(OnboardingError) as exc:
            collect_credential_blob("grok", src)
        assert exc.value.code == "provider_mismatch"

    def test_missing_credential_file(self, tmp_path: Path) -> None:
        src_dir = tmp_path / "empty"
        src_dir.mkdir()
        with pytest.raises(OnboardingError) as exc:
            collect_credential_blob("grok", src_dir)
        assert exc.value.code == "missing_credential_file"

    def test_nonexistent_source(self, tmp_path: Path) -> None:
        with pytest.raises(OnboardingError) as exc:
            collect_credential_blob("grok", tmp_path / "nope")
        assert exc.value.code == "invalid_source"

    def test_symlink_file_rejected(self, tmp_path: Path) -> None:
        real = _write(tmp_path / "real.json", '{"token": "x"}')
        link = tmp_path / "link.json"
        link.symlink_to(real)
        with pytest.raises(OnboardingError) as exc:
            collect_credential_blob("grok", link)
        assert exc.value.code == "unsafe_path"

    def test_symlinked_dir_component_rejected(self, tmp_path: Path) -> None:
        outside = _write(tmp_path / "outside" / "auth.json", '{"token": "x"}')
        src_dir = tmp_path / "grokhome"
        (src_dir / ".grok").mkdir(parents=True)
        (src_dir / ".grok" / "auth.json").symlink_to(outside)
        with pytest.raises(OnboardingError) as exc:
            collect_credential_blob("grok", src_dir)
        assert exc.value.code == "unsafe_path"

    def test_open_permissions_rejected_by_default(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", '{"token": "x"}', mode=0o644)
        with pytest.raises(OnboardingError) as exc:
            collect_credential_blob("grok", src)
        assert exc.value.code == "bad_permissions"
        blob = collect_credential_blob("grok", src, allow_open_permissions=True)
        assert blob["files"] == {GROK_AUTH_REL: '{"token": "x"}'}

    def test_non_regular_file_rejected(self, tmp_path: Path) -> None:
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo, 0o600)
        with pytest.raises(OnboardingError) as exc:
            collect_credential_blob("grok", fifo)
        assert exc.value.code == "invalid_source"

    def test_schema_mismatch_json(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", "not json at all")
        with pytest.raises(OnboardingError) as exc:
            collect_credential_blob("codex", src)
        assert exc.value.code == "schema_mismatch"

    def test_schema_mismatch_toml(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "credentials.toml", "key = [unclosed")
        with pytest.raises(OnboardingError) as exc:
            collect_credential_blob("devin", src)
        assert exc.value.code == "schema_mismatch"

    def test_schema_rejects_non_object_json(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", '["a", "b"]')
        with pytest.raises(OnboardingError) as exc:
            collect_credential_blob("grok", src)
        assert exc.value.code == "schema_mismatch"

    def test_undeclared_path_in_blob_rejected(self) -> None:
        blob = {"provider": "grok", "files": {".ssh/id_rsa": "x"}}
        with pytest.raises(OnboardingError) as exc:
            validate_credential_blob("grok", blob)
        assert exc.value.code == "unsafe_path"

    def test_escaping_path_rejected(self) -> None:
        blob = {"provider": "grok", "files": {"../outside": "x"}}
        with pytest.raises(OnboardingError) as exc:
            validate_credential_blob("grok", blob)
        assert exc.value.code == "unsafe_path"
        blob = {"provider": "grok", "files": {"/etc/passwd": "x"}}
        with pytest.raises(OnboardingError) as exc:
            validate_credential_blob("grok", blob)
        assert exc.value.code == "unsafe_path"

    def test_empty_blob_files_rejected(self) -> None:
        with pytest.raises(OnboardingError) as exc:
            validate_credential_blob("grok", {"provider": "grok", "files": {}})
        assert exc.value.code == "invalid_blob"

    def test_add_rolls_back_record_when_blob_write_fails(self, tmp_path: Path) -> None:
        class BadBlobStore(InMemoryAccountStore):
            def put_blob(self, account_id: str, blob: dict) -> None:
                raise RuntimeError("store on fire")

        src = _write(tmp_path / "auth.json", '{"token": "x"}')
        svc = _service(BadBlobStore())
        with pytest.raises(RuntimeError):
            svc.add("grok", src, account_id="g1")
        # No active, credential-less account left for the scheduler to pick.
        assert _registry(svc).get("g1") is None


class ScriptedProbe:
    """Deterministic CredentialProbe for tests."""

    def __init__(self, status: str, detail: str = "") -> None:
        self.result = ProbeResult(status, detail)
        self.calls: list[tuple[str, bool]] = []

    def probe(self, account: Account, blob: dict | None) -> ProbeResult:
        self.calls.append((account.id, blob is not None))
        return self.result


class TestVerify:
    def _account(self, svc: OnboardingService, tmp_path: Path, provider: str = "grok") -> Account:
        src = _write(tmp_path / "auth.json", '{"token": "x"}')
        return svc.add(provider, src, experimental_ok=True)

    def test_static_probe_ok(self, tmp_path: Path) -> None:
        svc = _service(probe=StaticCredentialProbe())
        account = self._account(svc, tmp_path)
        result, updated = svc.verify(account.id)
        assert result.status == "ok"
        assert updated.status == "active"
        assert updated.last_error is None

    def test_static_probe_invalid_blob(self, tmp_path: Path) -> None:
        svc = _service(probe=StaticCredentialProbe())
        account = self._account(svc, tmp_path)
        _registry(svc).store.put_blob(account.id, {"provider": "grok", "files": {"bad": "y"}})
        result, updated = svc.verify(account.id)
        assert result.status == "invalid_blob"
        assert updated.status == "invalid"
        assert updated.last_error is not None

    def test_no_credential_marks_invalid(self) -> None:
        svc = _service(probe=StaticCredentialProbe())
        _registry(svc).put(Account(id="bare", provider="grok", label="bare", secret_name=""))
        result, updated = svc.verify("bare")
        assert result.status == "no_credential"
        assert updated.status == "invalid"

    def test_secret_only_account_is_probe_unavailable(self) -> None:
        svc = _service(probe=StaticCredentialProbe())
        _registry(svc).put(
            Account(id="sec", provider="grok", label="sec", secret_name="sbx-acct-sec")
        )
        result, updated = svc.verify("sec")
        assert result.status == "probe_unavailable"
        assert updated.status == "active"
        assert updated.last_error == "probe_unavailable"

    def test_auth_invalid_excluded_from_auto_scheduling(self, tmp_path: Path) -> None:
        svc = _service(probe=ScriptedProbe("auth_invalid"))
        account = self._account(svc, tmp_path)
        result, updated = svc.verify(account.id)
        assert result.status == "auth_invalid"
        assert updated.status == "invalid"

        scheduler = AccountScheduler(_registry(svc))
        decision = scheduler.decide(provider="grok", account="auto")
        assert decision.account is None
        assert decision.error == "provider_exhausted"

    def test_provider_unavailable_keeps_status(self, tmp_path: Path) -> None:
        svc = _service(probe=ScriptedProbe("provider_unavailable"))
        account = self._account(svc, tmp_path)
        result, updated = svc.verify(account.id)
        assert result.status == "provider_unavailable"
        assert updated.status == "active"
        assert updated.last_error == "provider_unavailable"

    def test_ok_probe_does_not_reenable_disabled(self, tmp_path: Path) -> None:
        svc = _service(probe=ScriptedProbe("ok"))
        account = self._account(svc, tmp_path)
        svc.disable(account.id)
        result, updated = svc.verify(account.id)
        assert result.status == "ok"
        assert updated.status == "disabled"

    def test_verify_unknown_account(self) -> None:
        svc = _service()
        with pytest.raises(OnboardingError) as exc:
            svc.verify("nope")
        assert exc.value.code == "account_not_found"


class TestSandboxVerifyProbe:
    """The verify seam against LocalProcessBackend + stub_runner (no real creds)."""

    def _probe(self, stub_runner: Path) -> SandboxVerifyProbe:
        return SandboxVerifyProbe(LocalProcessBackend(), [sys.executable, str(stub_runner)])

    def test_init_ok(self, tmp_path: Path, stub_runner: Path) -> None:
        backend = LocalProcessBackend()
        probe = SandboxVerifyProbe(backend, [sys.executable, str(stub_runner)])
        svc = _service(probe=probe)
        src = _write(tmp_path / "auth.json", f'{{"token": "{SECRET}"}}')
        account = svc.add("codex", src)
        result, updated = svc.verify(account.id)
        assert result.status == "ok"
        assert updated.status == "active"
        # Throwaway sandbox is destroyed after the probe.
        assert backend.list() == []

    def test_unsupported_provider_maps_init_failed(self, tmp_path: Path, stub_runner: Path) -> None:
        backend = LocalProcessBackend()
        probe = SandboxVerifyProbe(backend, [sys.executable, str(stub_runner)])
        svc = _service(probe=probe)
        src = _write(tmp_path / "creds.json", '{"token": "x"}')
        account = svc.add("claude", src, experimental_ok=True)
        result, updated = svc.verify(account.id)
        # stub_runner argparse rejects --provider claude -> init_failed -> invalid.
        assert result.status == "init_failed"
        assert updated.status == "invalid"
        assert updated.last_error == "init_failed"
        assert backend.list() == []

    def test_probe_error_maps_provider_unavailable(self, tmp_path: Path) -> None:
        class DeadBackend:
            def create(self, spec: Any) -> Any:
                raise RuntimeError("no sandboxes")

        svc = _service(probe=SandboxVerifyProbe(DeadBackend(), ["runner"]))
        src = _write(tmp_path / "auth.json", '{"token": "x"}')
        account = svc.add("grok", src)
        result, updated = svc.verify(account.id)
        assert result.status == "provider_unavailable"
        assert updated.status == "active"


class TestRefresh:
    def test_refresh_replaces_blob_atomically(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", '{"token": "old"}')
        svc = _service()
        account = svc.add("grok", src)

        new_blob = {"provider": "grok", "files": {GROK_AUTH_REL: '{"token": "new"}'}}
        blob_file = _write(tmp_path / "blob.json", json.dumps(new_blob))
        outcome = svc.refresh(account.id, blob_file)
        assert outcome == {"changed": True, "files": 1}
        # Next sandbox restore uses the refreshed credential.
        assert _registry(svc).get_credential_blob(account.id) == new_blob

    def test_refresh_unchanged_when_identical(self, tmp_path: Path) -> None:
        content = '{"token": "same"}'
        src = _write(tmp_path / "auth.json", content)
        svc = _service()
        account = svc.add("grok", src)
        outcome = svc.refresh(account.id, src)
        assert outcome == {"changed": False, "files": 1}

    def test_failed_refresh_preserves_last_good(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", '{"token": "good"}')
        svc = _service()
        account = svc.add("grok", src)
        good = _registry(svc).get_credential_blob(account.id)

        bad = _write(tmp_path / "bad.json", "not json")
        with pytest.raises(OnboardingError):
            svc.refresh(account.id, bad)
        assert _registry(svc).get_credential_blob(account.id) == good

    def test_provider_mismatch_refresh_preserves(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", '{"token": "good"}')
        svc = _service()
        account = svc.add("grok", src)
        good = _registry(svc).get_credential_blob(account.id)

        wrong = _write(
            tmp_path / "wrong.json",
            json.dumps({"provider": "codex", "files": {CODEX_AUTH_REL: "{}"}}),
        )
        with pytest.raises(OnboardingError) as exc:
            svc.refresh(account.id, wrong)
        assert exc.value.code == "provider_mismatch"
        assert _registry(svc).get_credential_blob(account.id) == good

    def test_refresh_via_stdin_blob(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", '{"token": "old"}')
        svc = _service()
        account = svc.add("grok", src)
        new_blob = {"provider": "grok", "files": {GROK_AUTH_REL: '{"token": "new"}'}}
        svc.refresh(account.id, "-", stdin_text=json.dumps(new_blob))
        assert _registry(svc).get_credential_blob(account.id) == new_blob

    def test_refresh_unknown_account(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", '{"token": "x"}')
        svc = _service()
        with pytest.raises(OnboardingError) as exc:
            svc.refresh("nope", src)
        assert exc.value.code == "account_not_found"


class TestExport:
    def test_export_writes_blob_mode_600(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", f'{{"token": "{SECRET}"}}')
        svc = _service()
        account = svc.add("grok", src)
        out = svc.export(account.id, tmp_path / "out" / "blob.json")
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
        assert json.loads(out.read_text(encoding="utf-8")) == _registry(svc).get_credential_blob(
            account.id
        )

    def test_export_refuses_symlink_target(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", '{"token": "x"}')
        svc = _service()
        account = svc.add("grok", src)
        real = _write(tmp_path / "real.json", "{}")
        link = tmp_path / "link.json"
        link.symlink_to(real)
        with pytest.raises(OnboardingError) as exc:
            svc.export(account.id, link)
        assert exc.value.code == "unsafe_path"

    def test_export_no_credential(self) -> None:
        svc = _service()
        _registry(svc).put(Account(id="bare", provider="grok", label="b"))
        with pytest.raises(OnboardingError) as exc:
            svc.export("bare", "out.json")
        assert exc.value.code == "no_credential"


class TestLifecycle:
    def test_disable_enable(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", '{"token": "x"}')
        svc = _service()
        account = svc.add("grok", src)
        assert svc.disable(account.id).status == "disabled"
        assert svc.enable(account.id).status == "active"

    def test_remove_requires_confirmation(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", '{"token": "x"}')
        svc = _service()
        account = svc.add("grok", src)
        with pytest.raises(OnboardingError) as exc:
            svc.remove(account.id)
        assert exc.value.code == "confirmation_required"
        svc.remove(account.id, confirm=True)
        assert _registry(svc).get(account.id) is None
        assert _registry(svc).get_credential_blob(account.id) is None

    def test_remove_refuses_running_account(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", '{"token": "x"}')
        svc = _service()
        account = svc.add("grok", src)
        _registry(svc).set_running(account.id, 1)
        with pytest.raises(OnboardingError) as exc:
            svc.remove(account.id, confirm=True)
        assert exc.value.code == "account_in_use"
        assert _registry(svc).get(account.id) is not None

    def test_remove_unknown_account(self) -> None:
        svc = _service()
        with pytest.raises(OnboardingError) as exc:
            svc.remove("nope", confirm=True)
        assert exc.value.code == "account_not_found"


class TestAccountIdSafety:
    """account_id feeds FileAccountStore paths and the sbx-acct-<id> Secret
    name — anything outside [A-Za-z0-9._-] is refused before any store I/O."""

    BAD_IDS = ("../escape", "..", "/abs", "a/b", "a\\b", "white space", ".hidden")

    def test_add_rejects_unsafe_account_id(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "auth.json", '{"token": "x"}')
        svc = _service(FileAccountStore(tmp_path / "store"))
        for bad in self.BAD_IDS:
            with pytest.raises(OnboardingError) as exc:
                svc.add("grok", src, account_id=bad)
            assert exc.value.code == "invalid_account_id"
        assert svc.list() == []

    def test_add_traversal_writes_nothing_outside_store(self, tmp_path: Path) -> None:
        svc = _service(FileAccountStore(tmp_path / "store"))
        src = _write(tmp_path / "auth.json", '{"token": "x"}')
        with pytest.raises(OnboardingError):
            svc.add("grok", src, account_id="../escape")
        assert not (tmp_path / "escape.json").exists()

    def test_remove_rejects_traversal_id(self, tmp_path: Path) -> None:
        store_dir = tmp_path / "store"
        svc = _service(FileAccountStore(store_dir))
        src = _write(tmp_path / "auth.json", '{"token": "x"}')
        svc.add("grok", src)  # materializes store dirs
        victim = store_dir / "victim.json"  # == store/accounts/../victim.json
        victim.write_text(json.dumps({"id": "../victim", "provider": "grok", "label": "x"}))
        with pytest.raises(OnboardingError) as exc:
            svc.remove("../victim", confirm=True)
        assert exc.value.code == "invalid_account_id"
        assert victim.is_file()

    def test_lookup_commands_reject_unsafe_ids(self, tmp_path: Path) -> None:
        svc = _service()
        for call in (svc.status, svc.verify, svc.disable, svc.enable):
            with pytest.raises(OnboardingError) as exc:
                call("../x")
            assert exc.value.code == "invalid_account_id"
        with pytest.raises(OnboardingError) as exc:
            svc.refresh("../x", tmp_path / "nope.json")
        assert exc.value.code == "invalid_account_id"
        with pytest.raises(OnboardingError) as exc:
            svc.export("../x", tmp_path / "out.json")
        assert exc.value.code == "invalid_account_id"


class TestCli:
    def test_end_to_end_no_secret_leak(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_dir = tmp_path / "store"
        creds = _write(tmp_path / "auth.json", f'{{"token": "{SECRET}"}}')

        def run(*argv: str) -> tuple[int, str, str]:
            rc = onboarding_main(["--store-dir", str(store_dir), *argv])
            captured = capsys.readouterr()
            assert SECRET not in captured.out
            assert SECRET not in captured.err
            return rc, captured.out, captured.err

        rc, out, _ = run("providers")
        assert rc == 0
        for provider in ("codex", "devin", "antigravity", "grok", "opencode", "claude"):
            assert provider in out

        rc, out, _ = run(
            "add",
            "--provider",
            "grok",
            "--from",
            str(creds),
            "--account-id",
            "g1",
            "--label",
            "grok-01",
            "--slots",
            "2",
        )
        assert rc == 0 and "added g1" in out

        rc, out, _ = run("list")
        assert rc == 0 and "g1" in out and "grok-01" in out

        rc, out, _ = run("status", "g1", "--json")
        assert rc == 0
        entry = json.loads(out)
        assert entry["has_credential"] is True
        assert entry["credential_files"] == [GROK_AUTH_REL]
        assert entry["max_concurrent"] == 2

        rc, out, _ = run("verify", "g1")
        assert rc == 0 and "probe=ok" in out

        new_blob = _write(
            tmp_path / "blob.json",
            json.dumps({"provider": "grok", "files": {GROK_AUTH_REL: '{"t": "n"}'}}),
        )
        rc, out, _ = run("refresh", "g1", "--from", str(new_blob))
        assert rc == 0 and "replaced" in out

        rc, out, _ = run("export", "g1", "--out", str(tmp_path / "blob-out.json"))
        assert rc == 0 and "exported g1" in out

        rc, _, _ = run("remove", "g1")
        assert rc == 2  # missing --yes
        rc, _, _ = run("disable", "g1")
        assert rc == 0
        rc, out, _ = run("remove", "g1", "--yes")
        assert rc == 0 and "removed g1" in out

        rc, out, _ = run("list", "--json")
        assert json.loads(out)["accounts"] == []

    def test_cli_providers_output_uses_release_tiers(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression: ``providers`` must not print ``stable`` for providers
        the release matrix marks Experimental/Preview/unsupported."""
        rc = onboarding_main(["--store-dir", str(tmp_path / "store"), "providers"])
        assert rc == 0
        rows = {
            line.split("\t")[0]: line.split("\t")[1]
            for line in capsys.readouterr().out.splitlines()
            if "\t" in line
        }
        assert rows["opencode"] == "preview"
        assert rows["devin"] == "experimental"
        assert rows["claude"] == "unsupported"
        assert rows["codex"] == "stable"
        assert "stable" not in {rows["opencode"], rows["devin"], rows["grok"]}

    def test_describe_reports_support_tier(self, tmp_path: Path) -> None:
        svc = _service()
        src = _write(tmp_path / "auth.json", '{"token": "x"}')
        account = svc.add("opencode", src)
        assert svc.describe(account)["support"] == "preview"

    def test_cli_error_is_structured(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = onboarding_main(
            [
                "--store-dir",
                str(tmp_path / "store"),
                "add",
                "--provider",
                "bogus",
                "--from",
                str(tmp_path),
            ]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "error[unknown_provider]" in err

    def test_cli_experimental_rejected_without_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        creds = _write(tmp_path / "creds.json", '{"token": "x"}')
        rc = onboarding_main(
            [
                "--store-dir",
                str(tmp_path / "store"),
                "add",
                "--provider",
                "claude",
                "--from",
                str(creds),
            ]
        )
        assert rc == 2
        assert "error[experimental_provider]" in capsys.readouterr().err

    def test_cli_persists_to_file_store(self, tmp_path: Path) -> None:
        store_dir = tmp_path / "store"
        creds = _write(tmp_path / "auth.json", '{"token": "x"}')
        rc = onboarding_main(
            [
                "--store-dir",
                str(store_dir),
                "add",
                "--provider",
                "grok",
                "--from",
                str(creds),
                "--account-id",
                "g1",
            ]
        )
        assert rc == 0
        # Credential file on disk is 0600; the record has no credential material.
        blob_path = store_dir / "credentials" / "g1.json"
        assert stat.S_IMODE(blob_path.stat().st_mode) == 0o600
        record = json.loads((store_dir / "accounts" / "g1.json").read_text(encoding="utf-8"))
        assert "token" not in json.dumps(record) or "files" not in record


class TestFileStoreIntegration:
    def test_refresh_atomic_on_file_store(self, tmp_path: Path) -> None:
        store_dir = tmp_path / "store"
        svc = _service(FileAccountStore(store_dir))
        src = _write(tmp_path / "auth.json", '{"token": "v1"}')
        account = svc.add("grok", src)
        new_blob = {"provider": "grok", "files": {GROK_AUTH_REL: '{"token": "v2"}'}}
        svc.refresh(account.id, "-", stdin_text=json.dumps(new_blob))
        # A fresh registry instance sees only the refreshed blob.
        reopened = OnboardingService(PersistentAccountRegistry(FileAccountStore(store_dir)))
        assert reopened.registry.get_credential_blob(account.id) == new_blob
