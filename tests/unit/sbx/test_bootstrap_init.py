"""``sbx init``: checks report actionable hints; config write is idempotent."""

from __future__ import annotations

from pathlib import Path

from sbx_fakes import FakePlane, make_cfg, make_env

import sbx.init as init_mod
import sbx.prereqs as prereqs
from sbx.config import BootstrapConfig, load


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
    report = init_mod.init(cfg, FakePlane(), env=env, providers=("codex",))
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

    report = init_mod.init(
        cfg, FakePlane(), env=env, providers=("codex",), verify=True, auth_check=auth_check
    )
    assert seen == []  # nothing to verify without a discovered file

    auth = Path(env["HOME"]) / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text("{}")
    auth.chmod(0o600)
    report = init_mod.init(
        cfg, FakePlane(), env=env, providers=("codex",), verify=True, auth_check=auth_check
    )
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


class TestCheckGithub:
    """SOR-117: advisory GitHub-bridge detection — names/statuses only."""

    _NO_GH = staticmethod(lambda name: None)

    def test_token_detected_not_opted_in_warns_without_secret(self) -> None:
        check = prereqs.check_github({"GH_TOKEN": "REDACTED_GITHUB"}, which=self._NO_GH)
        assert not check.ok and check.warn
        assert "GH_TOKEN" in check.detail
        assert "REDACTED_GITHUB" not in f"{check.detail} {check.hint}"
        assert "SBX_GITHUB_EPHEMERAL" in (check.hint or "")

    def test_opted_in_with_token_is_ok(self) -> None:
        env = {"GH_TOKEN": "REDACTED_GITHUB", "SBX_GITHUB_EPHEMERAL": "1"}
        check = prereqs.check_github(env, which=self._NO_GH)
        assert check.ok and "GH_TOKEN" in check.detail
        assert "REDACTED_GITHUB" not in check.detail

    def test_opted_in_without_token_warns(self) -> None:
        check = prereqs.check_github({"SBX_GITHUB_EPHEMERAL": "1"}, which=self._NO_GH)
        assert not check.ok and check.warn
        assert "GH_TOKEN" in (check.hint or "")

    def test_nothing_detected_is_ok_and_neutral(self) -> None:
        check = prereqs.check_github({}, which=self._NO_GH)
        assert check.ok and "disabled" in check.detail

    def test_gh_authenticated_under_verify(self) -> None:
        class _Proc:
            returncode = 0

        check = prereqs.check_github(
            {}, verify=True, runner=lambda *a, **k: _Proc(), which=lambda n: "/usr/bin/gh"
        )
        assert not check.ok and check.warn
        assert "gh CLI is authenticated" in check.detail
        assert "gh auth token" in (check.hint or "")

    def test_armed_with_named_secret_is_ok_without_host_token(self) -> None:
        """SOR-133: a config-armed bridge + named Secret is satisfied without
        a host-side token — the remote control plane reads it from Modal."""
        check = prereqs.check_github({}, which=self._NO_GH, gate=True, secret_name="sbx-github")
        assert check.ok and "sbx-github" in check.detail

    def test_named_secret_without_gate_warns(self) -> None:
        check = prereqs.check_github({}, which=self._NO_GH, gate=False, secret_name="sbx-github")
        assert not check.ok and check.warn
        assert "sbx-github" in check.detail
        assert "SBX_GITHUB_EPHEMERAL" in (check.hint or "")


def test_init_reports_github_bridge_advisory(tmp_path) -> None:
    env = make_env(tmp_path)
    cfg = make_cfg(tmp_path, env=env, write=False)
    report = init_mod.init(cfg, FakePlane(), env=env)
    gh = next(c for c in report.checks if c.name == "github")
    assert gh.ok  # nothing detected: advisory-neutral, not a failure


def test_init_persists_github_bridge_flags(tmp_path) -> None:
    """SOR-133: ``--github``/``--github-secret`` persist the gate + Secret
    name, and the advisory reflects the just-written resolved config."""
    env = make_env(tmp_path)
    cfg = make_cfg(tmp_path, env=env, write=False)
    report = init_mod.init(cfg, FakePlane(), env=env, github=True, github_secret="sbx-github")
    reloaded = load(tmp_path / "config.toml", env={}).config
    assert reloaded.github_ephemeral is True
    assert reloaded.github_secret_name == "sbx-github"
    gh = next(c for c in report.checks if c.name == "github")
    assert gh.ok and "sbx-github" in gh.detail


def test_init_no_github_flag_disarms_persisted_gate(tmp_path) -> None:
    env = make_env(tmp_path)
    cfg = make_cfg(
        tmp_path,
        env=env,
        config=BootstrapConfig(github_ephemeral=True, github_secret_name="sbx-github"),
    )
    init_mod.init(cfg, FakePlane(), env=env, github=False)
    reloaded = load(tmp_path / "config.toml", env={}).config
    assert reloaded.github_ephemeral is False
    assert reloaded.github_secret_name == "sbx-github"  # name kept; gate off
