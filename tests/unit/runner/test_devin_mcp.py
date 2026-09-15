"""SOR-77: task-scoped Linear MCP config generation for Devin workers.

Covers the runner half of the contract: ``SBX_LINEAR_API_KEY`` in the worker
env makes ``runner init`` emit ``~/.config/devin/mcp_config.json`` (mode 600)
with an ``${env:}``-indirected Authorization header plus an
``mcp__linear__*`` permission grant, while the raw key never lands on disk,
in session.json, or in the event stream.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from runtime.runner import mcp
from runtime.runner.adapters.devin import DevinAdapter
from runtime.runner.codex import child_env
from runtime.runner.events import redact_line, redact_obj
from tests.unit.runner.conftest import load_json, run_runner

LINEAR_KEY = "lin_api_REDACTED_TEST_VALUE"
DEVIN_MODEL = "swe-2-high"
CRED_TOML = 'windsurf_api_key = "REDACTED"\napi_server_url = "https://server.codeium.com"\n'


def _devin_env(env: dict[str, str]) -> dict[str, str]:
    env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {"provider": "devin", "files": {".local/share/devin/credentials.toml": CRED_TOML}}
    )
    return env


def _init_devin(env: dict[str, str]):
    return run_runner(["init", "--provider", "devin", "--model", DEVIN_MODEL], env)


def test_linear_mcp_enabled_requires_key() -> None:
    assert not mcp.linear_mcp_enabled({})
    assert not mcp.linear_mcp_enabled({mcp.LINEAR_API_KEY_ENV: ""})
    assert mcp.linear_mcp_enabled({mcp.LINEAR_API_KEY_ENV: LINEAR_KEY})


def test_write_linear_mcp_config_noop_without_key(tmp_path: Path) -> None:
    assert mcp.write_linear_mcp_config(tmp_path, env={}) is None
    assert not mcp.mcp_config_path(tmp_path).exists()


def test_write_linear_mcp_config_shape_and_mode(tmp_path: Path) -> None:
    path = mcp.write_linear_mcp_config(tmp_path, env={mcp.LINEAR_API_KEY_ENV: LINEAR_KEY})
    assert path == tmp_path / ".config" / "devin" / "mcp_config.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    data = json.loads(path.read_text(encoding="utf-8"))
    entry = data["mcpServers"]["linear"]
    assert entry["url"] == "https://mcp.linear.app/mcp"
    assert entry["transport"] == "http"
    assert entry["headers"]["Authorization"] == "Bearer ${env:SBX_LINEAR_API_KEY}"

    # The generated file must contain the env indirection, never the raw key.
    assert LINEAR_KEY not in path.read_text(encoding="utf-8")
    assert "SBX_LINEAR_API_KEY" in path.read_text(encoding="utf-8")


def test_write_linear_mcp_config_preserves_other_servers(tmp_path: Path) -> None:
    path = mcp.mcp_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"mcpServers": {"other": {"url": "https://example.com/mcp"}}}) + "\n",
        encoding="utf-8",
    )
    mcp.write_linear_mcp_config(tmp_path, env={mcp.LINEAR_API_KEY_ENV: LINEAR_KEY})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["other"]["url"] == "https://example.com/mcp"
    assert data["mcpServers"]["linear"]["url"] == "https://mcp.linear.app/mcp"


def test_prepare_home_grants_linear_permissions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(mcp.LINEAR_API_KEY_ENV, LINEAR_KEY)
    home = tmp_path / "home"
    DevinAdapter().prepare_home(home, DEVIN_MODEL)

    config = json.loads((home / ".config" / "devin" / "config.json").read_text(encoding="utf-8"))
    assert "mcp__linear__*" in config["permissions"]["allow"]
    assert mcp.mcp_config_path(home).is_file()
    assert stat.S_IMODE(mcp.mcp_config_path(home).stat().st_mode) == 0o600


def test_prepare_home_without_key_leaves_config_clean(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(mcp.LINEAR_API_KEY_ENV, raising=False)
    home = tmp_path / "home"
    DevinAdapter().prepare_home(home, DEVIN_MODEL)

    config = json.loads((home / ".config" / "devin" / "config.json").read_text(encoding="utf-8"))
    assert "permissions" not in config
    assert not mcp.mcp_config_path(home).exists()


def test_init_devin_with_linear_key(work: Path, runner_env: dict[str, str]) -> None:
    env = _devin_env(runner_env)
    env[mcp.LINEAR_API_KEY_ENV] = LINEAR_KEY
    result = _init_devin(env)
    assert result.returncode == 0, result.stderr

    home = work / "home"
    mcp_cfg = json.loads(mcp.mcp_config_path(home).read_text(encoding="utf-8"))
    assert mcp_cfg["mcpServers"]["linear"]["url"] == "https://mcp.linear.app/mcp"

    config = json.loads((home / ".config" / "devin" / "config.json").read_text(encoding="utf-8"))
    assert "mcp__linear__*" in config["permissions"]["allow"]

    session = load_json(work / "session.json")
    assert session["mcp_servers"] == ["linear"]

    # Raw key never serialized: not in session.json, events, or on disk.
    assert LINEAR_KEY not in (work / "session.json").read_text(encoding="utf-8")
    assert LINEAR_KEY not in (work / "events.jsonl").read_text(encoding="utf-8")
    assert LINEAR_KEY not in (work / "events.raw.jsonl").read_text(encoding="utf-8")
    for path in work.rglob("*"):
        if path.is_file():
            assert LINEAR_KEY not in path.read_text(encoding="utf-8", errors="replace"), (
                f"Linear key leaked into {path}"
            )


def test_init_devin_without_linear_key_unchanged(work: Path, runner_env: dict[str, str]) -> None:
    env = _devin_env(runner_env)
    env.pop(mcp.LINEAR_API_KEY_ENV, None)
    result = _init_devin(env)
    assert result.returncode == 0, result.stderr
    session = load_json(work / "session.json")
    assert session["mcp_servers"] == []
    assert not mcp.mcp_config_path(work / "home").exists()


def test_init_codex_ignores_linear_key(work: Path, runner_env: dict[str, str]) -> None:
    runner_env.pop("SBX_ACCOUNT_CREDENTIAL", None)
    runner_env[mcp.LINEAR_API_KEY_ENV] = LINEAR_KEY
    result = run_runner(["init", "--model", "gpt-5.6-luna"], runner_env)
    assert result.returncode == 0, result.stderr
    session = load_json(work / "session.json")
    assert session["mcp_servers"] == []
    assert not mcp.mcp_config_path(work / "home").exists()


def test_child_env_passes_linear_key_to_agent(work: Path, monkeypatch) -> None:
    """``${env:SBX_LINEAR_API_KEY}`` resolves inside the ``devin acp`` child."""
    monkeypatch.setenv(mcp.LINEAR_API_KEY_ENV, LINEAR_KEY)
    env = child_env(work, work / ".codex")
    assert env[mcp.LINEAR_API_KEY_ENV] == LINEAR_KEY


def test_linear_key_redacted_from_event_stream() -> None:
    line = json.dumps(
        {
            "type": "tool_result",
            "headers": {"authorization": f"Bearer {LINEAR_KEY}"},
            "output": f"server replied Bearer {LINEAR_KEY}",
        }
    )
    safe = redact_line(line)
    assert LINEAR_KEY not in safe
    obj = redact_obj({"headers": {"Authorization": f"Bearer {LINEAR_KEY}"}})
    assert LINEAR_KEY not in json.dumps(obj)
