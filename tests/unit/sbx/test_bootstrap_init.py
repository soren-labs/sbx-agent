"""``sbx init``: checks report actionable hints; config write is idempotent."""

from __future__ import annotations

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
