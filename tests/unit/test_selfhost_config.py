"""Self-hosted operator config (`sbx server`): private file, precedence, isolation, checks."""

from __future__ import annotations

import base64
import os
import socket
import stat
from pathlib import Path

import psycopg
import pytest
from control.selfhost import bootstrap, checks
from control.selfhost import config as cfg

from sbx.sdk import diagnostics

ZERO_VAULT = "k1:" + base64.b64encode(bytes(32)).decode()
ZERO_MASTER = "00" * 32


def _write(path: Path, **values: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cfg.render(values))
    os.chmod(path, 0o600)
    return path


def _valid(**extra: str) -> dict[str, str]:
    return {
        "SBX_DATABASE_URL": "postgresql://localhost/sbx",
        "SBX_VAULT_KEYS": ZERO_VAULT,
        "SBX_RUNTIME_MASTER_KEY": ZERO_MASTER,
        **extra,
    }


def test_default_paths_follow_xdg(tmp_path: Path) -> None:
    assert cfg.default_config_path() == Path(os.environ["XDG_CONFIG_HOME"]) / "sbx" / "server.env"
    assert cfg.default_data_dir() == Path(os.environ["XDG_DATA_HOME"]) / "sbx"


def test_explicit_config_env_selects_the_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(cfg.CONFIG_ENV, str(tmp_path / "other.env"))
    assert cfg.default_config_path() == tmp_path / "other.env"


def test_render_parse_round_trip_and_unknown_keys_rejected() -> None:
    values = _valid(SBX_LISTEN_PORT="18800")
    assert cfg.parse(cfg.render(values)) == values
    with pytest.raises(cfg.ConfigError, match="line 1"):
        cfg.parse("OPENAI_API_KEY=REDACTED\n")
    with pytest.raises(cfg.ConfigError):
        cfg.parse("SBX_DATABASE_URL\n")


def test_write_new_is_private_and_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "conf" / "server.env"
    cfg.write_new(path, _valid())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    before = path.read_text()
    with pytest.raises(cfg.ConfigError, match="already exists") as exc:
        cfg.write_new(path, _valid(SBX_VAULT_KEYS="k2:" + ZERO_VAULT[3:]))
    assert "vault keys" in exc.value.action
    assert path.read_text() == before


def test_environment_overrides_file_and_sources_never_show_secrets(tmp_path: Path) -> None:
    path = _write(tmp_path / "server.env", **_valid(SBX_LISTEN_PORT="18801"))
    settings = cfg.ServerSettings.load(path, environ={"SBX_LISTEN_PORT": "18802"})
    assert settings.port == 18802
    assert settings.sources["SBX_LISTEN_PORT"] == "environment"
    assert settings.sources["SBX_VAULT_KEYS"] == "file"
    described = settings.describe()["settings"]
    assert described["SBX_VAULT_KEYS"] == {"source": "file", "value": "(set)"}
    assert ZERO_VAULT not in repr(settings.describe())
    # A config file implies the private XDG data dir rather than ./.sbx-data.
    assert settings.data_dir == cfg.default_data_dir()


def test_separate_config_never_inherits_another_configs_settings(tmp_path: Path) -> None:
    """--config B must not pick up optional settings of the default config A."""
    default = _write(
        tmp_path / "a" / "server.env",
        **_valid(SBX_RESEND_API_KEY="REDACTED", SBX_MAIL_FROM="SBX <no-reply@a.example>"),
    )
    selected = _write(
        tmp_path / "b" / "server.env",
        **_valid(SBX_DATA_DIR=str(tmp_path / "b-data"), SBX_LISTEN_PORT="18803"),
    )
    settings = cfg.ServerSettings.load(selected, environ={})
    # The parent shell still points at A (and carries values once exported from A).
    base = {
        "PATH": "/usr/bin",
        cfg.CONFIG_ENV: str(default),
        "SBX_RESEND_API_KEY": "REDACTED",
        "SBX_MAIL_FROM": "SBX <no-reply@a.example>",
    }
    child = settings.child_env(base)
    assert child[cfg.CONFIG_ENV] == str(selected)
    assert "SBX_RESEND_API_KEY" not in child and "SBX_MAIL_FROM" not in child
    assert child["SBX_DATA_DIR"] == str(tmp_path / "b-data")
    assert child["SBX_LISTEN_PORT"] == "18803" and child["PATH"] == "/usr/bin"


def test_problems_name_the_key_and_fix() -> None:
    assert cfg.problems(_valid()) == []
    keys = {key for key, _, _ in cfg.problems({})}
    assert keys == set(cfg.REQUIRED_KEYS)
    found = cfg.problems(_valid(SBX_RESEND_API_KEY="REDACTED"))
    assert [k for k, _, _ in found] == ["SBX_MAIL_FROM"]
    assert cfg.vault_spec_error(ZERO_VAULT + "," + ZERO_VAULT.replace("k1", "k0")) is None


def test_config_check_flags_a_readable_config_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "server.env", **_valid(SBX_DATA_DIR=str(tmp_path / "data")))
    os.chmod(path, 0o644)
    found = checks.config_checks(cfg.ServerSettings.load(path, environ={}))
    failure = checks.first_failure(found)
    assert failure is not None and failure.name == "config_file"
    assert failure.category == "config" and failure.action == f"chmod 600 {path}"
    assert checks.exit_code(found) == 3


def test_missing_config_file_points_at_init(tmp_path: Path) -> None:
    settings = cfg.ServerSettings.load(tmp_path / "absent.env", environ={})
    failure = checks.first_failure(checks.config_checks(settings))
    assert failure is not None and failure.action == "run `sbx server init`"
    env_only = cfg.ServerSettings.load(tmp_path / "absent.env", environ=_valid())
    assert checks.first_failure(checks.config_checks(env_only)) is None


def test_port_in_use_is_a_config_failure(tmp_path: Path) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        settings = cfg.ServerSettings.load(
            tmp_path / "x.env", environ=_valid(SBX_LISTEN_PORT=str(port))
        )
        found = checks.port_check(settings)
    assert found.status == "fail" and found.category == "config"
    assert "already in use" in found.message


class _Fake(psycopg.OperationalError):
    def __init__(self, text: str, sqlstate: str | None = None) -> None:
        self._state = sqlstate
        super().__init__(text)

    @property
    def sqlstate(self) -> str | None:  # type: ignore[override]
        return self._state


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (_Fake('FATAL: password authentication failed for user "sbx"'), "authentication"),
        (_Fake('FATAL: Peer authentication failed for user "nobody"'), "authentication"),
        (_Fake("boom", "28P01"), "authentication"),
        (_Fake('FATAL: database "nosuch" does not exist'), "config"),
        (_Fake("connection refused\n\tIs the server running"), "reachability"),
        (psycopg.ProgrammingError('missing "=" after "x" in connection info string'), "config"),
    ],
)
def test_database_errors_are_classified(error: Exception, category: str) -> None:
    assert checks.classify_connect_error(error)[0] == category


def test_failure_order_and_exit_codes_match_the_sdk() -> None:
    assert checks.EXIT_CODES == diagnostics.EXIT_CODES
    assert checks.CATEGORIES == diagnostics.CATEGORIES
    found = [
        checks.Check("x", "executor", "fail", "m"),
        checks.Check("y", "authentication", "fail", "m"),
        checks.Check("z", "config", "warn", "m"),
    ]
    assert checks.first_failure(found).category == "authentication"
    assert checks.exit_code(found) == 5


def test_local_cli_names_match_the_harness_adapters(repo_root: Path) -> None:
    for provider, binary in checks.LOCAL_HARNESS_CLIS.items():
        source = (repo_root / "runtime" / "harnesses" / f"{provider}.py").read_text()
        assert f'"{binary}")' in source, provider


def test_init_refuses_a_data_dir_that_already_holds_instance_data(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "blobs").mkdir(parents=True)
    path = tmp_path / "new.env"
    with pytest.raises(cfg.ConfigError, match="already holds SBX data") as exc:
        bootstrap.initialize(path, data_dir=data, database_url="postgresql://localhost/sbx")
    assert "--config" in exc.value.action
    assert not path.exists(), "no new keys are written next to old data"


def test_init_with_external_database_writes_fresh_keys(tmp_path: Path) -> None:
    path = tmp_path / "conf" / "server.env"
    result = bootstrap.initialize(
        path,
        data_dir=tmp_path / "data",
        database_url="host=/nonexistent-sbx-socket dbname=sbx connect_timeout=1",
        port=18804,
        executors="local",
    )
    assert result["database"] == {"managed": False}
    assert result["warnings"], "an unreachable database is reported, not hidden"
    values = cfg.parse(path.read_text())
    assert cfg.problems(values) == []
    assert values["SBX_LISTEN_PORT"] == "18804" and "SBX_MANAGED_POSTGRES" not in values
    assert values["SBX_VAULT_KEYS"] not in repr(result)
    assert values["SBX_RUNTIME_MASTER_KEY"] not in repr(result)
    second = bootstrap.initialize(
        tmp_path / "other.env",
        data_dir=tmp_path / "data2",
        database_url="host=/nonexistent-sbx-socket dbname=sbx connect_timeout=1",
    )
    assert (
        cfg.parse((tmp_path / "other.env").read_text())["SBX_VAULT_KEYS"]
        != values["SBX_VAULT_KEYS"]
    )
    assert second["config_file"] == str(tmp_path / "other.env")
