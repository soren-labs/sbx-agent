"""Single config source + env overrides (SOR-98 must-deliver #1)."""

from __future__ import annotations

import pytest
from control.config import (
    ACCOUNTS_DICT_NAME,
    MODAL_APP_NAME,
    RUNTIME_IMAGE_NAME,
    V1_BOOTSTRAP_SECRET_NAME,
)
from sbx.config import BootstrapConfig, load, load_file_values, save
from sbx_fakes import make_cfg, make_env


def test_defaults_come_from_control_config(tmp_path) -> None:
    cfg = make_cfg(tmp_path, write=False)
    assert cfg.config.modal_app_name == MODAL_APP_NAME
    assert cfg.config.accounts_dict == ACCOUNTS_DICT_NAME
    assert cfg.config.bootstrap_secret == V1_BOOTSTRAP_SECRET_NAME
    assert cfg.config.image_codex == RUNTIME_IMAGE_NAME
    assert cfg.config.providers == ("codex",)
    assert all(source == "default" for source in cfg.sources.values())
    assert cfg.file_exists is False


def test_save_and_reload_roundtrip(tmp_path) -> None:
    config = BootstrapConfig(
        modal_profile="acme",
        api_base_url="https://acme--sbx-control-fastapi-app.modal.run",
        providers=("codex", "devin"),
    )
    path = tmp_path / "config.toml"
    save(config, path)
    cfg = load(path, env={})
    assert cfg.file_exists
    assert cfg.config == config
    assert cfg.sources["modal_profile"] == "file"
    assert cfg.sources["providers"] == "file"


def test_env_overrides_beat_file(tmp_path) -> None:
    save(
        BootstrapConfig(modal_profile="file-profile", modal_app_name="file-app"),
        tmp_path / "c.toml",
    )
    cfg = load(
        tmp_path / "c.toml",
        env={"SBX_MODAL_APP_NAME": "env-app", "MODAL_PROFILE": "env-profile"},
    )
    assert cfg.config.modal_app_name == "env-app"
    assert cfg.config.modal_profile == "env-profile"
    assert cfg.sources["modal_app_name"] == "env"
    assert cfg.sources["modal_profile"] == "env"


def test_env_providers_and_base_url(tmp_path) -> None:
    cfg = load(
        tmp_path / "missing.toml",
        env={"SBX_PROVIDERS": "codex, devin", "SBX_BASE_URL": "https://x.modal.run"},
    )
    assert cfg.config.providers == ("codex", "devin")
    assert cfg.config.api_base_url == "https://x.modal.run"


def test_load_file_values_ignores_env(tmp_path) -> None:
    save(BootstrapConfig(modal_profile="file-profile"), tmp_path / "c.toml")
    values = load_file_values(tmp_path / "c.toml")
    assert values.modal_profile == "file-profile"
    # init must not freeze env overrides into the file
    env_cfg = load(tmp_path / "c.toml", env={"SBX_MODAL_PROFILE": "env-profile"})
    assert env_cfg.config.modal_profile == "env-profile"


def test_config_paths_from_env(tmp_path) -> None:
    env = make_env(tmp_path)
    cfg = load(env=env)
    assert cfg.path == tmp_path / "config.toml"


def test_max_concurrent_env_and_file(tmp_path) -> None:
    cfg = load(tmp_path / "missing.toml", env={"SBX_MAX_CONCURRENT": "4"})
    assert cfg.config.max_concurrent == 4
    assert cfg.sources["max_concurrent"] == "env"
    save(BootstrapConfig(max_concurrent=6), tmp_path / "c.toml")
    cfg = load(tmp_path / "c.toml", env={})
    assert cfg.config.max_concurrent == 6
    assert cfg.sources["max_concurrent"] == "file"


def test_max_concurrent_unset_stays_absent(tmp_path) -> None:
    """An unset cap must not leak into the file or the deploy env."""
    config = BootstrapConfig()
    save(config, tmp_path / "c.toml")
    text = (tmp_path / "c.toml").read_text()
    assert "max_concurrent" not in text
    assert "SBX_MAX_CONCURRENT" not in config.deploy_env()


def test_max_concurrent_reaches_deploy_env(tmp_path) -> None:
    env = BootstrapConfig(max_concurrent=3).deploy_env()
    assert env["SBX_MAX_CONCURRENT"] == "3"


def test_max_concurrent_rejects_nonpositive(tmp_path) -> None:
    with pytest.raises(ValueError):
        load(tmp_path / "missing.toml", env={"SBX_MAX_CONCURRENT": "0"})
    with pytest.raises(ValueError):
        load(tmp_path / "missing.toml", env={"SBX_MAX_CONCURRENT": "bogus"})
