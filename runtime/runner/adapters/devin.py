"""Devin CLI adapter (SOR-72).

Transport: the pinned Devin CLI's ``-p`` print mode emits only the final
assistant text — no session id and no streaming events — so the adapter
drives the official ACP stdio protocol (``devin acp``, JSON-RPC) through the
internal bridge ``runtime.runner.adapters.devin_acp``. The bridge emits the
native NDJSON line protocol defined here (the same shape
``tests/fakes/fake_devin.py`` and ``tests/fixtures/events/devin/*.jsonl``
use); ``translate`` normalises it to canonical Codex-shaped events.

``DEVIN_BIN`` overrides the CLI binary (the bridge spawns
``DEVIN_BIN acp``; tests point it at fakes). ``SBX_DEVIN_TRANSPORT=cli``
switches to direct ``devin -p`` argv for the ``fake_devin.py`` NDJSON fake.

Forward compatibility (SOR-80): any parseable JSON object of an unknown
``type`` — or a known type whose payload maps to no canonical event —
yields ``{"type": "sbx.noop"}`` (dropped by ``runner turn``). ``[]`` is
returned only for lines with no JSON object (unparseable / non-object),
which the runner counts as a bad line.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from runtime.runner import mcp
from runtime.runner.constants import NOOP_EVENT_TYPE
from runtime.runner.events import parse_event_line
from runtime.runner.workspace import atomic_write

Health = Literal["ok", "auth_invalid", "rate_limited", "unknown"]

CREDENTIALS_TOML = ".local/share/devin/credentials.toml"

_AUTH_NEEDLES = (
    "401",
    "403",
    "unauthorized",
    "unauthenticated",
    "invalid api key",
    "invalid token",
    "credential",
    "auth",
    "not logged in",
    "login required",
    "forbidden",
)
_RATE_NEEDLES = (
    "429",
    "rate_limit",
    "rate limit",
    "too many requests",
    "quota",
    "concurrency",
    "concurrent",
    "busy",
    "unavailable",
    "exhausted",
)

# Native tool names (ACP ``_meta.cognition.ai/inferenceToolName`` or fake
# ``tool`` field) that map to a canonical ``file_change`` item; everything
# else maps to ``command_execution``.
_FILE_TOOLS = {"write", "edit", "str_replace", "multi_edit", "apply_patch", "notebook_edit"}
_EXEC_TOOLS = {"exec", "shell", "bash", "run", "command"}

# native usage key -> canonical usage key (events.md usage_mapping +
# ACP camelCase fields from ``session/prompt`` results).
_USAGE_MAP = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cached_input_tokens",
    "cache_write_tokens": "cache_write_input_tokens",
    "thinking_tokens": "reasoning_output_tokens",
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cachedReadTokens": "cached_input_tokens",
    "cacheWriteTokens": "cache_write_input_tokens",
    "thinkingTokens": "reasoning_output_tokens",
}

_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

_NOOP = {"type": NOOP_EVENT_TYPE}


def devin_bin_tokens() -> list[str]:
    raw = os.environ.get("DEVIN_BIN", "devin")
    tokens = shlex.split(raw)
    if not tokens:
        tokens = ["devin"]
    if len(tokens) == 1 and tokens[0].endswith(".py"):
        return [sys.executable, tokens[0]]
    return tokens


def use_acp() -> bool:
    """Whether to drive the CLI through the ACP stdio bridge.

    ACP is the default transport for ``provider=devin`` (the pinned CLI's
    ``-p`` mode exposes no session id / stream shape). Tests select the
    direct ``devin -p`` argv shape with ``SBX_DEVIN_TRANSPORT=cli`` plus a
    ``DEVIN_BIN`` fake.
    """
    return os.environ.get("SBX_DEVIN_TRANSPORT", "acp") != "cli"


def _bridge_argv(prompt: str, model: str, session_id: str | None) -> list[str]:
    argv = [sys.executable, "-m", "runtime.runner.adapters.devin_acp"]
    if session_id:
        argv += ["--resume", session_id]
    elif model:
        argv += ["--model", model]
    argv += ["--", prompt]
    return argv


def _map_usage(raw: dict[str, Any]) -> dict[str, int]:
    usage = {field: 0 for field in _USAGE_FIELDS}
    for key, value in raw.items():
        canonical = _USAGE_MAP.get(key)
        if canonical is None:
            continue
        try:
            usage[canonical] = int(value)
        except (TypeError, ValueError):
            continue
    return usage


def _text_content(content: Any) -> str:
    """Join the text fragments of an ACP/native ``content`` array."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        inner = block.get("content")
        if isinstance(inner, dict) and isinstance(inner.get("text"), str):
            parts.append(inner["text"])
        elif isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


class DevinAdapter:
    """Devin CLI adapter: ACP stdio transport, canonical event translation."""

    provider = "devin"
    credential_files: tuple[str, ...] = (CREDENTIALS_TOML,)

    def __init__(self) -> None:
        self._stream_text_ids: set[str] = set()
        self._item_seq = 0
        self._open_tools: dict[str, dict[str, Any]] = {}
        self._last_tool_id: str | None = None

    def prepare_home(self, home: Path, model: str) -> None:
        """Write ``~/.config/devin/config.json`` (model + completed setup).

        The credential blob restores ``.local/share/devin/credentials.toml``
        under ``home``; ``prepare_home`` never touches it.
        """
        cfg_dir = home / ".config" / "devin"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (home / ".local" / "share" / "devin").mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / "config.json"
        data: dict[str, Any] = {}
        if cfg_path.is_file():
            try:
                loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, dict):
                data = loaded
        data.setdefault("version", 1)
        agent = data.setdefault("agent", {})
        if model:
            agent["model"] = model
        data.setdefault("shell", {}).setdefault("setup_complete", True)
        mcp_names = mcp.session_server_names()
        if mcp_names:
            mcp.apply_mcp_permissions(data, mcp_names)
        atomic_write(cfg_path, json.dumps(data, indent=2) + "\n")
        if mcp_names:
            mcp.write_session_mcp_config(home)

    def first_turn_argv(self, prompt: str, model: str) -> list[str]:
        if use_acp():
            return _bridge_argv(prompt, model, None)
        return [*devin_bin_tokens(), "-p", prompt]

    def resume_argv(self, prompt: str, native_session_id: str) -> list[str]:
        if use_acp():
            return _bridge_argv(prompt, "", native_session_id)
        return [*devin_bin_tokens(), "--resume", native_session_id, "-p", prompt]

    def translate(self, raw_line: str) -> list[dict[str, Any]]:
        obj, bad = parse_event_line(raw_line)
        if bad or obj is None:
            return []
        event_type = obj.get("type")
        if event_type == "session.started":
            session_id = obj.get("session_id")
            if isinstance(session_id, str) and session_id:
                return [{"type": "thread.started", "thread_id": session_id}]
            return [_NOOP]
        if event_type == "turn.started":
            return [{"type": "turn.started"}]
        if event_type in ("assistant_message", "reasoning") and obj.get("id"):
            text = obj.get("text")
            if not isinstance(text, str) or not text:
                return [_NOOP]
            item_id = str(obj["id"])
            completed = obj.get("status") == "completed"
            stage = (
                "item.completed"
                if completed
                else ("item.updated" if item_id in self._stream_text_ids else "item.started")
            )
            self._stream_text_ids.add(item_id)
            return [
                {
                    "type": stage,
                    "item": {
                        "id": item_id,
                        "type": "agent_message"
                        if event_type == "assistant_message"
                        else "reasoning",
                        "text": text,
                    },
                }
            ]
        if event_type == "assistant_message":
            text = obj.get("text")
            if not isinstance(text, str) or not text:
                return [_NOOP]
            return [
                {
                    "type": "item.completed",
                    "item": {
                        "id": self._next_item_id(),
                        "type": "agent_message",
                        "text": text,
                    },
                }
            ]
        if event_type == "reasoning":
            text = obj.get("text")
            if not isinstance(text, str) or not text:
                return [_NOOP]
            return [
                {
                    "type": "item.completed",
                    "item": {
                        "id": self._next_item_id(),
                        "type": "reasoning",
                        "text": text,
                    },
                }
            ]
        if event_type == "tool_call":
            return [self._tool_event(obj, started=True)]
        if event_type in ("tool_result", "tool_update"):
            return [self._tool_event(obj, started=False)]
        if event_type == "turn.completed":
            usage = obj.get("usage")
            return [
                {
                    "type": "turn.completed",
                    "usage": _map_usage(usage) if isinstance(usage, dict) else _map_usage({}),
                }
            ]
        if event_type == "turn.failed":
            message = self._error_message(obj)
            return [{"type": "turn.failed", "error": {"message": message}}]
        if event_type == "error":
            return [{"type": "error", "message": self._error_message(obj)}]
        # Forward compatibility: a parseable object of an unknown type is
        # acknowledged as NOOP, never a bad line. ``[]`` is reserved for
        # lines with no JSON object (unparseable / non-object), which the
        # runner counts as bad JSON.
        return [_NOOP]

    def extract_session_id(self, events: Iterable[dict[str, Any]]) -> str | None:
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "thread.started":
                thread_id = ev.get("thread_id")
                if isinstance(thread_id, str) and thread_id:
                    return thread_id
        return None

    def health_from(self, exit_code: int | None, stderr_tail: str) -> Health:
        if exit_code == 0:
            return "ok"
        tail = (stderr_tail or "").lower()
        if any(needle in tail for needle in _AUTH_NEEDLES):
            return "auth_invalid"
        if any(needle in tail for needle in _RATE_NEEDLES):
            return "rate_limited"
        return "unknown"

    # -- internals ---------------------------------------------------------

    def _next_item_id(self) -> str:
        item_id = f"item_{self._item_seq}"
        self._item_seq += 1
        return item_id

    @staticmethod
    def _is_file_tool(obj: dict[str, Any]) -> bool:
        tool = str(obj.get("tool") or "").lower()
        kind = str(obj.get("kind") or "").lower()
        return tool in _FILE_TOOLS or kind == "edit"

    def _tool_event(self, obj: dict[str, Any], *, started: bool) -> dict[str, Any]:
        item_id = obj.get("id")
        if not isinstance(item_id, str) or not item_id:
            item_id = self._last_tool_id or self._next_item_id()
        status = str(obj.get("status") or ("in_progress" if started else "completed"))
        event_type = "item.started" if started else "item.completed"
        if not started and status not in ("completed", "failed"):
            event_type = "item.updated"
        tool = str(obj.get("tool") or "")
        raw_input = obj.get("input") if isinstance(obj.get("input"), dict) else {}
        prior = self._open_tools.get(item_id, {})
        if started:
            merged = dict(obj)
            merged.setdefault("input", raw_input)
            self._open_tools[item_id] = merged
            self._last_tool_id = item_id
        else:
            if not raw_input and isinstance(prior.get("input"), dict):
                raw_input = prior["input"]
            tool = tool or str(prior.get("tool") or "")
            if not obj.get("kind") and prior.get("kind"):
                obj = {**obj, "kind": prior["kind"], "tool": tool}
        if self._is_file_tool({**obj, "tool": tool}):
            path = raw_input.get("file_path") or raw_input.get("path") or obj.get("path")
            item = {
                "id": item_id,
                "type": "file_change",
                "changes": [{"path": str(path or ""), "kind": "update"}],
                "status": "in_progress" if started else status,
            }
        else:
            command = raw_input.get("command") or prior.get("title") or obj.get("title") or tool
            item = {
                "id": item_id,
                "type": "command_execution",
                "command": str(command or ""),
                "aggregated_output": "" if started else str(obj.get("output") or ""),
                "status": "in_progress" if started else status,
                "exit_code": None if started else obj.get("exit_code"),
            }
        if not started and item_id in self._open_tools:
            del self._open_tools[item_id]
        return {"type": event_type, "item": item}

    @staticmethod
    def _error_message(obj: dict[str, Any]) -> str:
        error = obj.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(obj.get("message"), str):
            return obj["message"]
        return "provider error"
