"""Single config source + env overrides (SOR-98 must-deliver #1)."""

from __future__ import annotations

import pytest
from control.config import (
    ACCOUNTS_DICT_NAME,
    MODAL_APP_NAME,
    RUNTIME_IMAGE_NAME,
    V1_BOOTSTRAP_SECRET_NAME,
)
from sbx.config import (
    BootstrapConfig,
    load,
    load_file_values,
    save,
    validate_providers,
)
from sbx.errors import BootstrapError
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


def test_github_bridge_persists_via_file(tmp_path) -> None:
    """SOR-133: gate + Secret *name* round-trip through config.toml."""
    config = BootstrapConfig(github_ephemeral=True, github_secret_name="sbx-github")
    path = tmp_path / "c.toml"
    save(config, path)
    text = path.read_text()
    assert "[github]" in text
    assert "ephemeral = true" in text
    assert 'secret_name = "sbx-github"' in text
    cfg = load(path, env={})
    assert cfg.config == config
    assert cfg.sources["github_ephemeral"] == "file"
    assert cfg.sources["github_secret_name"] == "file"


def test_github_bridge_env_overrides_file(tmp_path) -> None:
    save(
        BootstrapConfig(github_ephemeral=True, github_secret_name="file-secret"),
        tmp_path / "c.toml",
    )
    cfg = load(
        tmp_path / "c.toml",
        env={"SBX_GITHUB_EPHEMERAL": "0", "SBX_GITHUB_SECRET_NAME": "env-secret"},
    )
    assert cfg.config.github_ephemeral is False
    assert cfg.config.github_secret_name == "env-secret"
    assert cfg.sources["github_ephemeral"] == "env"
    assert cfg.sources["github_secret_name"] == "env"


def test_github_bridge_env_arms_without_file(tmp_path) -> None:
    cfg = load(
        tmp_path / "missing.toml",
        env={"SBX_GITHUB_EPHEMERAL": "1", "SBX_GITHUB_SECRET_NAME": "sbx-github"},
    )
    assert cfg.config.github_ephemeral is True
    assert cfg.config.github_secret_name == "sbx-github"


def test_github_bridge_rejects_nonboolean(tmp_path) -> None:
    with pytest.raises(ValueError):
        load(tmp_path / "missing.toml", env={"SBX_GITHUB_EPHEMERAL": "maybe"})


def test_github_bridge_reaches_deploy_env() -> None:
    """Resolved gate + name replay into the remote env — never a token."""
    env = BootstrapConfig(github_ephemeral=True, github_secret_name="sbx-github").deploy_env()
    assert env["SBX_GITHUB_EPHEMERAL"] == "1"
    assert env["SBX_GITHUB_SECRET_NAME"] == "sbx-github"
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env


def test_github_bridge_defaults_stay_out_of_deploy_env() -> None:
    env = BootstrapConfig().deploy_env()
    assert "SBX_GITHUB_EPHEMERAL" not in env
    assert "SBX_GITHUB_SECRET_NAME" not in env


def test_github_secret_name_not_a_managed_secret() -> None:
    """The bridge Secret is operator-managed — ``secret_names()`` must not
    claim it (uninstall would otherwise delete it on --purge-credentials)."""
    cfg = BootstrapConfig(github_ephemeral=True, github_secret_name="sbx-github")
    assert "sbx-github" not in cfg.secret_names()


def test_secret_names_are_provider_aware() -> None:
    """SOR-116: the shared Codex Secret is required iff codex is enabled."""
    codex = BootstrapConfig(providers=("codex",))
    assert codex.secret_names() == (
        "sbx-codex-auth",
        "sbx-basic-auth",
        "sbx-v1-bootstrap",
    )
    devin = BootstrapConfig(providers=("devin",))
    assert "sbx-codex-auth" not in devin.secret_names()
    assert devin.secret_names() == ("sbx-basic-auth", "sbx-v1-bootstrap")
    mixed = BootstrapConfig(providers=("devin", "codex"))
    assert "sbx-codex-auth" in mixed.secret_names()


def test_deploy_env_forwards_provider_set() -> None:
    env = BootstrapConfig(providers=("codex", "devin")).deploy_env()
    assert env["SBX_PROVIDERS"] == "codex,devin"


def test_validate_providers_accepts_known_set() -> None:
    validate_providers(("codex",))
    validate_providers(("devin", "grok"))


def test_validate_providers_rejects_empty() -> None:
    with pytest.raises(BootstrapError) as exc:
        validate_providers(())
    assert exc.value.code == "invalid_providers"
    assert "deploy.providers" in exc.value.message


def test_validate_providers_rejects_unknown() -> None:
    with pytest.raises(BootstrapError) as exc:
        validate_providers(("codex", "bogus"))
    assert exc.value.code == "invalid_providers"
    assert "bogus" in exc.value.message
    assert "codex" in (exc.value.hint or "")
