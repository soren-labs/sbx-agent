"""``sbx init``: checks report actionable hints; config write is idempotent."""

from __future__ import annotations

from pathlib import Path

import sbx.init as init_mod
import sbx.prereqs as prereqs
from sbx.config import BootstrapConfig, load
from sbx_fakes import FakePlane, make_cfg, make_env


def test_init_writes_config_and_reports_checks(tmp_path) -> None:
    env = make_env(tmp_path)
    cfg = make_cfg(tmp_path, env=env, write=False)
    report = init_mod.init(cfg, FakePlane(), env=env, profile="acme")
    assert report.config_created
    assert (tmp_path / "config.toml").is_file()
    assert (tmp_path / "state").is_dir()
    assert load(tmp_path / "config.toml", env={}).config.modal_profile == "acme"
    names = [c.name for c in report.checks]
    assert "python" in names and "modal-cli" in names and "modal-auth" in names
    assert report.authenticated


def test_init_preserves_file_values_and_applies_flags(tmp_path) -> None:
    env = make_env(tmp_path)
    cfg = make_cfg(tmp_path, env=env, config=BootstrapConfig(modal_profile="keep-me"))
    init_mod.init(cfg, FakePlane(), env=env, app_name="custom-app")
    reloaded = load(tmp_path / "config.toml", env={}).config
    assert reloaded.modal_profile == "keep-me"  # file value preserved
    assert reloaded.modal_app_name == "custom-app"  # flag applied


def test_init_unauthenticated_reports_hint(tmp_path) -> None:
    env = make_env(tmp_path)
    cfg = make_cfg(tmp_path, env=env, write=False)
    report = init_mod.init(cfg, FakePlane(workspace=None), env=env)
    auth = next(c for c in report.checks if c.name == "modal-auth")
    assert not auth.ok
    assert auth.hint and "modal token new" in auth.hint


def test_init_scans_selected_provider_credentials(tmp_path) -> None:
    """Discovery runs for the selected providers only — findings are
    advisory warnings, never init-blocking failures (SOR-115)."""
    env = make_env(tmp_path)  # clean HOME: no credential files
    cfg = make_cfg(tmp_path, env=env, write=False)
    report = init_mod.init(cfg, FakePlane(), env=env, providers=("codex", "grok"))
    creds = {c.name: c for c in report.checks if c.name.startswith("cred:")}
    assert set(creds) == {"cred:codex", "cred:grok"}
    for check in creds.values():
        assert not check.ok and check.warn
        assert check.hint  # official login guidance attached


def test_init_found_credential_reports_discovered(tmp_path) -> None:
    env = make_env(tmp_path)
    auth = Path(env["HOME"]) / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"tokens": {"access_token": "REDACTED"}}')
    auth.chmod(0o600)
    cfg = make_cfg(tmp_path, env=env, write=False)
    report = init_mod.init(cfg, FakePlane(), env=env)
    scan = next(s for s in report.credentials if s.provider == "codex")
    assert scan.status == "discovered"
    check = next(c for c in report.checks if c.name == "cred:codex")
    assert check.ok


def test_init_verify_uses_injected_auth_check(tmp_path) -> None:
    env = make_env(tmp_path)
    cfg = make_cfg(tmp_path, env=env, write=False)
    seen: list[str] = []

    def auth_check(provider: str, home: Path) -> str:
        seen.append(provider)
        return "ok"

    report = init_mod.init(cfg, FakePlane(), env=env, verify=True, auth_check=auth_check)
    assert seen == []  # nothing to verify without a discovered file

    auth = Path(env["HOME"]) / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text("{}")
    auth.chmod(0o600)
    report = init_mod.init(cfg, FakePlane(), env=env, verify=True, auth_check=auth_check)
    assert seen == ["codex"]
    scan = report.credentials[0]
    assert scan.status == "verified"


def test_missing_tool_produces_actionable_hint(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(prereqs.shutil, "which", lambda name: None)
    checks = prereqs.tool_checks()
    git = next(c for c in checks if c.name == "git")
    assert not git.ok and git.warn and git.hint  # advisory, with remediation


def test_modal_package_missing_is_actionable(monkeypatch) -> None:
    monkeypatch.setattr(prereqs.importlib.util, "find_spec", lambda name: None)
    check = prereqs.check_modal_package()
    assert not check.ok and not check.warn
    assert check.hint and "modal token new" in check.hint


class TestModalAuthEnvTokens:
    """MODAL_TOKEN_ID/SECRET are a first-class auth source (SOR-115)."""

    def test_env_tokens_prove_auth_via_probe(self) -> None:
        env = {"MODAL_TOKEN_ID": "ak", "MODAL_TOKEN_SECRET": "as"}
        check = prereqs.check_modal_auth("env-ws", env=env)
        assert check.ok and "env MODAL_TOKEN_ID" in check.detail

    def test_env_tokens_with_failed_probe_suspect_the_tokens(self) -> None:
        env = {"MODAL_TOKEN_ID": "ak", "MODAL_TOKEN_SECRET": "as"}
        check = prereqs.check_modal_auth(None, env=env)
        assert not check.ok
        assert "MODAL_TOKEN_ID" in check.detail
        assert "valid" in (check.hint or "")

    def test_partial_env_tokens_call_out_the_missing_half(self) -> None:
        check = prereqs.check_modal_auth(None, env={"MODAL_TOKEN_ID": "ak"})
        assert not check.ok
        assert "MODAL_TOKEN_SECRET" in check.detail
        check = prereqs.check_modal_auth(None, env={"MODAL_TOKEN_SECRET": "as"})
        assert "MODAL_TOKEN_ID" in check.detail

    def test_no_auth_points_at_token_new(self) -> None:
        check = prereqs.check_modal_auth(None, env={})
        assert not check.ok and "modal token new" in (check.hint or "")
