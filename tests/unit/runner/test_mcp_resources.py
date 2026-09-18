"""SOR-129 runner-side session MCP resources.

``SBX_MCP_SERVERS`` carries resolved, registry-validated server entries
(config templates only — ``${env:VAR}`` indirection, never values). Devin's
``prepare_home`` writes ``mcp_config.json`` (mode 600) and grants
``mcp__<name>__*``; session.json records names only. Malformed payloads
fail closed (``EXIT_INTERNAL``); non-devin providers ignore the env because
the control plane refuses MCP for them before provisioning.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from runtime.runner import mcp
from tests.unit.runner.conftest import init_runner, load_json, run_runner

LINEAR_ENV_ENTRY = {
    "name": "linear",
    "url": "https://mcp.linear.app/mcp",
    "transport": "http",
    "headers": {"Authorization": "Bearer ${env:SBX_LINEAR_API_KEY}"},
}

NOTION_ENTRY = {
    "name": "notion",
    "url": "https://mcp.notion.com/mcp",
    "transport": "http",
    "headers": {"Authorization": "Bearer ${env:SBX_NOTION_API_KEY}"},
}


def _devin_env(runner_env: dict[str, str], work: Path, extra: dict | None = None) -> dict:
    env = dict(runner_env)
    env["HOME"] = str(work / "home")
    # No ambient account blob may leak into the devin init under test.
    env.pop("SBX_ACCOUNT_CREDENTIAL", None)
    if extra:
        env.update(extra)
    return env


def _init_devin(env: dict[str, str]):
    return run_runner(
        ["init", "--auth", "auth_json", "--model", "swe-2-high", "--provider", "devin"],
        env,
    )


class TestDeclaredMcpServers:
    def test_absent_env_is_empty(self) -> None:
        assert mcp.declared_mcp_servers({}) == []
        assert mcp.session_server_names({}) == []

    def test_parse_entries(self) -> None:
        env = {mcp.MCP_SERVERS_ENV: json.dumps([LINEAR_ENV_ENTRY, NOTION_ENTRY])}
        entries = mcp.declared_mcp_servers(env)
        assert [e["name"] for e in entries] == ["linear", "notion"]
        assert mcp.session_server_names(env) == ["linear", "notion"]

    def test_declared_linear_suppresses_gate_duplicate(self) -> None:
        env = {
            mcp.MCP_SERVERS_ENV: json.dumps([LINEAR_ENV_ENTRY]),
            mcp.LINEAR_API_KEY_ENV: "REDACTED",
        }
        assert mcp.session_server_names(env) == ["linear"]

    def test_linear_gate_still_appends(self) -> None:
        env = {
            mcp.MCP_SERVERS_ENV: json.dumps([NOTION_ENTRY]),
            mcp.LINEAR_API_KEY_ENV: "REDACTED",
        }
        assert mcp.session_server_names(env) == ["notion", "linear"]

    def test_duplicate_names_keep_first(self) -> None:
        dup = dict(NOTION_ENTRY)
        dup["url"] = "https://other.example.com/mcp"
        env = {mcp.MCP_SERVERS_ENV: json.dumps([NOTION_ENTRY, dup])}
        entries = mcp.declared_mcp_servers(env)
        assert len(entries) == 1
        assert entries[0]["url"] == NOTION_ENTRY["url"]

    @pytest.mark.parametrize(
        "payload",
        [
            "not-json{",
            json.dumps({"name": "linear"}),  # not a list
            json.dumps(["linear"]),  # entry not an object
            json.dumps([{"name": "no url!"}]),  # malformed name + missing url
            json.dumps([{"name": "x", "url": "https://ok", "headers": "nope"}]),
        ],
    )
    def test_malformed_payload_fails_closed(self, payload: str) -> None:
        with pytest.raises(mcp.McpConfigError):
            mcp.declared_mcp_servers({mcp.MCP_SERVERS_ENV: payload})


class TestWriteSessionMcpConfig:
    def test_entries_written_mode_600(self, work: Path) -> None:
        home = work / "home"
        env = {mcp.MCP_SERVERS_ENV: json.dumps([LINEAR_ENV_ENTRY, NOTION_ENTRY])}
        path = mcp.write_session_mcp_config(home, env=env)
        assert path == home / ".config" / "devin" / "mcp_config.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        data = load_json(path)
        servers = data["mcpServers"]
        assert servers["linear"]["url"] == "https://mcp.linear.app/mcp"
        assert servers["linear"]["headers"]["Authorization"] == ("Bearer ${env:SBX_LINEAR_API_KEY}")
        assert servers["notion"]["url"] == "https://mcp.notion.com/mcp"
        # Templates only — no raw token anywhere on disk.
        assert "REDACTED" not in path.read_text()

    def test_existing_unrelated_servers_preserved(self, work: Path) -> None:
        home = work / "home"
        cfg = home / ".config" / "devin" / "mcp_config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(
            json.dumps({"mcpServers": {"other": {"url": "https://other.example.com/mcp"}}})
        )
        mcp.write_session_mcp_config(home, env={mcp.MCP_SERVERS_ENV: json.dumps([NOTION_ENTRY])})
        servers = load_json(cfg)["mcpServers"]
        assert "other" in servers
        assert "notion" in servers

    def test_linear_gate_only_still_works(self, work: Path) -> None:
        home = work / "home"
        path = mcp.write_session_mcp_config(home, env={mcp.LINEAR_API_KEY_ENV: "REDACTED"})
        servers = load_json(path)["mcpServers"]
        assert servers["linear"]["headers"]["Authorization"] == ("Bearer ${env:SBX_LINEAR_API_KEY}")

    def test_noop_when_nothing_configured(self, work: Path) -> None:
        home = work / "home"
        assert mcp.write_session_mcp_config(home, env={}) is None


class TestApplyMcpPermissions:
    def test_grants_merged_once(self) -> None:
        config: dict = {}
        mcp.apply_mcp_permissions(config, ["linear", "notion"])
        mcp.apply_mcp_permissions(config, ["linear"])
        allow = config["permissions"]["allow"]
        assert allow == ["mcp__linear__*", "mcp__notion__*"]

    def test_existing_permissions_preserved(self) -> None:
        config = {"permissions": {"allow": ["mcp__github__*"]}}
        mcp.apply_mcp_permissions(config, ["linear"])
        assert config["permissions"]["allow"] == ["mcp__github__*", "mcp__linear__*"]


class TestRunnerInitDevin:
    def test_init_writes_config_permissions_and_session(self, runner_env: dict, work: Path) -> None:
        env = _devin_env(
            runner_env,
            work,
            {mcp.MCP_SERVERS_ENV: json.dumps([LINEAR_ENV_ENTRY, NOTION_ENTRY])},
        )
        result = _init_devin(env)
        assert result.returncode == 0, result.stderr

        session = load_json(work / "session.json")
        assert session["mcp_servers"] == ["linear", "notion"]
        # Names only — config templates never reach session.json.
        assert "mcp.linear.app" not in (work / "session.json").read_text()

        config = load_json(work / "home" / ".config" / "devin" / "config.json")
        allow = config["permissions"]["allow"]
        assert "mcp__linear__*" in allow
        assert "mcp__notion__*" in allow

        mcp_cfg = load_json(work / "home" / ".config" / "devin" / "mcp_config.json")
        assert set(mcp_cfg["mcpServers"]) == {"linear", "notion"}

    def test_init_malformed_mcp_env_fails_closed(self, runner_env: dict, work: Path) -> None:
        env = _devin_env(runner_env, work, {mcp.MCP_SERVERS_ENV: "not-json{"})
        result = _init_devin(env)
        assert result.returncode == 1  # EXIT_INTERNAL
        assert mcp.MCP_SERVERS_ENV in result.stderr

    def test_init_without_mcp_env_unchanged(self, runner_env: dict, work: Path) -> None:
        env = _devin_env(runner_env, work)
        env.pop(mcp.MCP_SERVERS_ENV, None)
        result = _init_devin(env)
        assert result.returncode == 0, result.stderr
        session = load_json(work / "session.json")
        assert session["mcp_servers"] == []
        assert not (work / "home" / ".config" / "devin" / "mcp_config.json").exists()

    def test_init_codex_ignores_mcp_env(self, runner_env: dict, work: Path) -> None:
        # The control plane refuses MCP for non-devin providers before
        # provisioning; if the env ever reached a codex init it is ignored.
        env = dict(runner_env)
        env.pop("SBX_ACCOUNT_CREDENTIAL", None)
        env[mcp.MCP_SERVERS_ENV] = json.dumps([LINEAR_ENV_ENTRY])
        result = init_runner(env)
        assert result.returncode == 0, result.stderr
        session = load_json(work / "session.json")
        assert session["mcp_servers"] == []
