"""Task-scoped MCP configuration for workers (SOR-77 Linear, SOR-129 refs).

Env/secret contract: the control plane injects ``SBX_LINEAR_API_KEY`` into
the worker environment through a Modal ``Secret.from_dict`` (never through
``SandboxSpec.env``), so the raw key stays out of API payloads, logs, git,
and session metadata. ``runner init`` then generates
``$HOME/.config/devin/mcp_config.json`` (mode 600) whose ``Authorization``
header references the variable via the Devin CLI's ``${env:VAR}``
interpolation — the raw key is never written to disk or serialized.

SOR-129 session resources: the control plane additionally hands resolved,
registry-validated MCP server entries to ``runner init`` via
``SBX_MCP_SERVERS`` (JSON list of ``{name, url, transport?, headers?}``).
Entries carry config templates only — credential material rides the same
``${env:VAR}`` indirection — so no secret value ever crosses the env or
lands on disk.
"""

from __future__ import annotations

import json
import os
import re
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

# SOR-129: resolved MCP server entries declared on the agent's create
# request (registry-validated by the control plane; config templates only).
MCP_SERVERS_ENV = "SBX_MCP_SERVERS"
_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_MCP_CONFIG_REL = Path(".config") / "devin" / "mcp_config.json"


class McpConfigError(Exception):
    """Malformed ``SBX_MCP_SERVERS`` payload — ``runner init`` fails closed."""


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


def _normalize_entry(item: Any) -> dict[str, Any]:
    """Validate one declared MCP entry → ``{name, url, transport, headers}``."""
    if not isinstance(item, dict):
        raise McpConfigError(f"{MCP_SERVERS_ENV} entries must be objects")
    name = item.get("name")
    if not isinstance(name, str) or not _MCP_NAME_RE.match(name):
        raise McpConfigError(f"{MCP_SERVERS_ENV} entry has a malformed name: {name!r}")
    url = item.get("url")
    if not isinstance(url, str) or not url.strip():
        raise McpConfigError(f"{MCP_SERVERS_ENV} entry {name!r} is missing a url")
    transport = item.get("transport") or "http"
    if not isinstance(transport, str) or not transport.strip():
        raise McpConfigError(f"{MCP_SERVERS_ENV} entry {name!r} has a malformed transport")
    headers = item.get("headers") or {}
    if not isinstance(headers, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()
    ):
        raise McpConfigError(f"{MCP_SERVERS_ENV} entry {name!r} has malformed headers")
    return {"name": name, "url": url, "transport": transport, "headers": dict(headers)}


def declared_mcp_servers(env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """MCP server entries the control plane declared for this worker (SOR-129).

    ``[]`` when the env is absent. A malformed payload raises
    :class:`McpConfigError` — init fails closed rather than partially
    configuring MCP. Duplicate names keep the first entry (the control
    plane already dedupes).
    """
    src = os.environ if env is None else env
    raw = src.get(MCP_SERVERS_ENV)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise McpConfigError(f"{MCP_SERVERS_ENV} is not valid JSON") from exc
    if not isinstance(data, list):
        raise McpConfigError(f"{MCP_SERVERS_ENV} must be a JSON list")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        entry = _normalize_entry(item)
        if entry["name"] in seen:
            continue
        seen.add(entry["name"])
        entries.append(entry)
    return entries


def session_server_names(env: Mapping[str, str] | None = None) -> list[str]:
    """All MCP server names this worker configures (declared + Linear gate)."""
    names = [entry["name"] for entry in declared_mcp_servers(env)]
    if linear_mcp_enabled(env) and LINEAR_SERVER_NAME not in names:
        names.append(LINEAR_SERVER_NAME)
    return names


def mcp_permission(name: str) -> str:
    """Headless pre-approval grant for one MCP server (``mcp__<name>__*``)."""
    return f"mcp__{name}__*"


def apply_mcp_permissions(config: dict[str, Any], names: list[str]) -> dict[str, Any]:
    """Merge ``mcp__<name>__*`` grants into ``permissions.allow`` (in place)."""
    permissions = config.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        permissions = {}
        config["permissions"] = permissions
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list):
        allow = []
        permissions["allow"] = allow
    for name in names:
        permission = mcp_permission(name)
        if permission not in allow:
            allow.append(permission)
    return config


def apply_linear_permissions(config: dict[str, Any]) -> dict[str, Any]:
    """Merge ``mcp__linear__*`` into ``permissions.allow`` (in place)."""
    return apply_mcp_permissions(config, [LINEAR_SERVER_NAME])


def _server_config_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """``mcpServers.<name>`` config body for a declared entry."""
    out: dict[str, Any] = {"url": entry["url"], "transport": entry["transport"]}
    if entry.get("headers"):
        out["headers"] = dict(entry["headers"])
    return out


def _write_mcp_config(home: Path, servers: Mapping[str, Any]) -> Path:
    path = mcp_config_path(home)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            data = loaded
    existing = data.get("mcpServers")
    if not isinstance(existing, dict):
        existing = {}
        data["mcpServers"] = existing
    existing.update(servers)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)
    return path


def write_linear_mcp_config(home: Path, *, env: Mapping[str, str] | None = None) -> Path | None:
    """Generate ``~/.config/devin/mcp_config.json`` with mode 600.

    No-op (returns ``None``) when ``SBX_LINEAR_API_KEY`` is absent. Existing
    servers are preserved; the ``linear`` entry is replaced wholesale.
    """
    if not linear_mcp_enabled(env):
        return None
    return _write_mcp_config(home, {LINEAR_SERVER_NAME: linear_server_entry()})


def write_session_mcp_config(home: Path, *, env: Mapping[str, str] | None = None) -> Path | None:
    """Generate ``mcp_config.json`` for every configured server (mode 600).

    SOR-129 declared entries plus the SOR-77 Linear gate (when
    ``SBX_LINEAR_API_KEY`` is present and no declared entry already owns
    the ``linear`` name). No-op (returns ``None``) when nothing is
    configured; existing unrelated servers are preserved.
    """
    servers = {entry["name"]: _server_config_entry(entry) for entry in declared_mcp_servers(env)}
    if linear_mcp_enabled(env) and LINEAR_SERVER_NAME not in servers:
        servers[LINEAR_SERVER_NAME] = linear_server_entry()
    if not servers:
        return None
    return _write_mcp_config(home, servers)
