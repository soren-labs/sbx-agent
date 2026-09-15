"""Task-scoped Linear MCP configuration for Devin workers (SOR-77).

Env/secret contract: the control plane injects ``SBX_LINEAR_API_KEY`` into
the worker environment through a Modal ``Secret.from_dict`` (never through
``SandboxSpec.env``), so the raw key stays out of API payloads, logs, git,
and session metadata. ``runner init`` then generates
``$HOME/.config/devin/mcp_config.json`` (mode 600) whose ``Authorization``
header references the variable via the Devin CLI's ``${env:VAR}``
interpolation — the raw key is never written to disk or serialized.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runtime.runner.workspace import atomic_write

LINEAR_API_KEY_ENV = "SBX_LINEAR_API_KEY"
LINEAR_SERVER_NAME = "linear"
LINEAR_MCP_URL = "https://mcp.linear.app/mcp"
# Headless workers cannot answer approval prompts; the task opted into
# Linear by carrying the key, so its tools are pre-approved wholesale.
LINEAR_MCP_PERMISSION = f"mcp__{LINEAR_SERVER_NAME}__*"

_MCP_CONFIG_REL = Path(".config") / "devin" / "mcp_config.json"


def linear_mcp_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether this worker was given a task-scoped Linear key."""
    src = os.environ if env is None else env
    return bool(src.get(LINEAR_API_KEY_ENV))


def mcp_config_path(home: Path) -> Path:
    return home / _MCP_CONFIG_REL


def linear_server_entry() -> dict[str, Any]:
    """``mcpServers.linear`` entry; auth resolves via ``${env:}`` at runtime."""
    return {
        "url": LINEAR_MCP_URL,
        "transport": "http",
        "headers": {"Authorization": f"Bearer ${{env:{LINEAR_API_KEY_ENV}}}"},
    }


def apply_linear_permissions(config: dict[str, Any]) -> dict[str, Any]:
    """Merge ``mcp__linear__*`` into ``permissions.allow`` (in place)."""
    permissions = config.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        permissions = {}
        config["permissions"] = permissions
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list):
        allow = []
        permissions["allow"] = allow
    if LINEAR_MCP_PERMISSION not in allow:
        allow.append(LINEAR_MCP_PERMISSION)
    return config


def write_linear_mcp_config(home: Path, *, env: Mapping[str, str] | None = None) -> Path | None:
    """Generate ``~/.config/devin/mcp_config.json`` with mode 600.

    No-op (returns ``None``) when ``SBX_LINEAR_API_KEY`` is absent. Existing
    servers are preserved; the ``linear`` entry is replaced wholesale.
    """
    if not linear_mcp_enabled(env):
        return None
    path = mcp_config_path(home)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            data = loaded
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    servers[LINEAR_SERVER_NAME] = linear_server_entry()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)
    return path
