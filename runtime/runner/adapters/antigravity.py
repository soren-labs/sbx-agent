"""Antigravity CLI adapter (SOR-62).

Drives ``agy -p ... --output-format stream-json`` per the verified SOR-60
spike (``spike/p2/AGY_SPIKE.md``): one process per turn, ``--conversation
<id>`` for resume, stdin closed by the runner. Native stream-json lines
(``init`` / ``step_update`` / ``result``) are normalised to canonical
Codex-shaped events (events.md §翻译规则映射).

``AGY_BIN`` overrides the CLI binary (tests point it at a replay helper).
Notable real-CLI behaviours handled here:

- ``init.conversation_id`` lives at the **top level** of the init line
  (the WP0 fake fixture nests it inside ``init``; both are accepted).
- ``result.error`` is a plain **string** (WP0 fixture uses an object; both
  are accepted).
- A stale ``--conversation`` id makes the CLI warn on stderr, exit 0, and
  silently open a **new** conversation. ``resume_argv`` records the
  requested id and ``translate`` suppresses ``thread.started`` when the
  reported ``conversation_id`` differs, so ``runner turn`` can detect the
  mismatch and fail the turn instead of forking the session.
- Recognised native lines that carry no canonical event (``system_message``
  steps, buffered deltas, usage-only DONE steps) — and any parseable JSON
  object of an unknown ``event`` kind (forward compatibility, SOR-80) —
  return ``{"type": NOOP_EVENT_TYPE}``: dropped by ``runner turn`` before
  ``events.jsonl`` and never counted as bad JSON. ``[]`` is returned only
  for lines with no JSON object (unparseable / non-object), which the
  runner counts as a bad line (events.md rule 5).
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from runtime.runner.constants import NOOP_EVENT_TYPE
from runtime.runner.events import parse_event_line
from runtime.runner.workspace import load_session, work_root

Health = Literal["ok", "auth_invalid", "rate_limited", "unknown"]

OAUTH_TOKEN_REL = ".gemini/antigravity-cli/antigravity-oauth-token"

_AUTH_NEEDLES = (
    "authentication required",
    "authentication failed",
    "unauthorized",
    "unauthenticated",
    "invalid token",
    "401",
    "auth",
)
_RATE_NEEDLES = (
    "429",
    "rate_limit",
    "rate limit",
    "too many requests",
    "quota",
    "resource_exhausted",
    "resource exhausted",
    "exhausted",
)

# native usage key -> canonical usage key (events.md usage_mapping).
_USAGE_MAP = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cached_input_tokens",
    "cache_write_tokens": "cache_write_input_tokens",
    "thinking_tokens": "reasoning_output_tokens",
}
_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

# step_type -> canonical item type for text-carrying steps.
_TEXT_STEPS = {"agent_response": "agent_message", "thinking": "reasoning"}

# step_type values that map to tool items ("tool" is the real 1.2.x name —
# verified on 1.2.2 and 1.2.3; "tool_call" only appears in the WP0
# hand-written fixtures).
_TOOL_STEPS = {"tool", "tool_call"}

# tool_name values that produce file_change items; all others produce
# command_execution items.
_FILE_TOOLS = {
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "write",
    "edit",
    "str_replace",
    "apply_patch",
}

_NOOP = {"type": NOOP_EVENT_TYPE}


def agy_bin_tokens() -> list[str]:
    raw = os.environ.get("AGY_BIN", "agy")
    tokens = shlex.split(raw)
    if not tokens:
        tokens = ["agy"]
    if len(tokens) == 1 and tokens[0].endswith(".py"):
        return [sys.executable, tokens[0]]
    return tokens


def _session_model() -> str:
    """Model recorded by ``runner init`` in session.json, for resume argv."""
    raw = load_session(work_root()).get("model")
    return raw if isinstance(raw, str) else ""


def _session_effort() -> str:
    """Canonical effort recorded by ``runner init`` in session.json (SOR-179)."""
    raw = load_session(work_root()).get("reasoning_effort")
    return raw if isinstance(raw, str) else ""


def _map_usage(raw: Any) -> dict[str, int]:
    usage = {field: 0 for field in _USAGE_FIELDS}
    if not isinstance(raw, dict):
        return usage
    for key, value in raw.items():
        canonical = _USAGE_MAP.get(key)
        if canonical is None:
            continue
        try:
            usage[canonical] = int(value)
        except (TypeError, ValueError):
            continue
    return usage


def _conversation_id(obj: dict[str, Any], inner: dict[str, Any] | None) -> str | None:
    """Native conversation id: top level (real CLI) or nested (WP0 fake)."""
    cid = obj.get("conversation_id")
    if isinstance(cid, str) and cid:
        return cid
    if isinstance(inner, dict):
        cid = inner.get("conversation_id")
        if isinstance(cid, str) and cid:
            return cid
    return None


def _error_message(result: dict[str, Any]) -> str:
    err = result.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str) and msg:
            return msg
        return json.dumps(err, ensure_ascii=False)
    if isinstance(err, str) and err:
        return err
    return "antigravity turn failed"


class AntigravityAdapter:
    """Antigravity (``agy``) adapter: stream-json NDJSON -> canonical events."""

    provider = "antigravity"
    credential_files: tuple[str, ...] = (OAUTH_TOKEN_REL,)

    def __init__(self) -> None:
        self._item_seq = 0
        self._expected_id: str | None = None
        self._stale = False
        self._thread_seen = False
        self._turn_started = False
        self._emitted_agent_message = False
        self._text_buf: dict[tuple[str, Any], list[str]] = {}
        self._open_tools: dict[Any, str] = {}

    def prepare_home(self, home: Path, model: str) -> None:
        """Create ``~/.gemini/antigravity-cli`` (700); keep the token at 600.

        The credential blob restores ``antigravity-oauth-token`` before this
        runs; ``prepare_home`` never writes its contents. Approval bypass is
        argv-level (``--dangerously-skip-permissions``), so no CLI config
        file is needed; ``model`` travels on argv, not in a config file.
        """
        gemini = home / ".gemini"
        cli_dir = gemini / "antigravity-cli"
        cli_dir.mkdir(parents=True, exist_ok=True)
        gemini.chmod(0o700)
        cli_dir.chmod(0o700)
        token = cli_dir / "antigravity-oauth-token"
        if token.is_file():
            token.chmod(0o600)

    def first_turn_argv(self, prompt: str, model: str) -> list[str]:
        argv = [*agy_bin_tokens(), "-p", prompt, "--output-format", "stream-json"]
        if model:
            argv += ["--model", model]
        effort = _session_effort()
        if effort:
            argv += ["--effort", effort]
        argv += ["--dangerously-skip-permissions", "--disable-slash-commands"]
        return argv

    def resume_argv(self, prompt: str, native_session_id: str) -> list[str]:
        self._expected_id = native_session_id
        argv = [
            *agy_bin_tokens(),
            "-p",
            prompt,
            "--conversation",
            native_session_id,
            "--output-format",
            "stream-json",
        ]
        model = _session_model()
        if model:
            argv += ["--model", model]
        effort = _session_effort()
        if effort:
            # Resume turns inherit the declared effort natively (SOR-179).
            argv += ["--effort", effort]
        argv += ["--dangerously-skip-permissions", "--disable-slash-commands"]
        return argv

    def translate(self, raw_line: str) -> list[dict[str, Any]]:
        obj, bad = parse_event_line(raw_line)
        if bad or obj is None:
            return []
        kind = obj.get("event")
        if kind == "init":
            return self._on_init(obj)
        if kind == "step_update":
            step = obj.get("step_update")
            return self._on_step(step) if isinstance(step, dict) else [_NOOP]
        if kind == "result":
            result = obj.get("result")
            return self._on_result(result) if isinstance(result, dict) else [_NOOP]
        # Forward compatibility: a parseable object of an unknown event
        # kind is acknowledged as NOOP, never a bad line. ``[]`` is
        # reserved for lines with no JSON object (unparseable /
        # non-object), which the runner counts as bad JSON.
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

    def _on_init(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        inner = obj.get("init")
        conv = _conversation_id(obj, inner if isinstance(inner, dict) else None)
        if not conv:
            return [_NOOP]
        if self._expected_id is not None and conv != self._expected_id:
            # Stale --conversation id: the CLI warned on stderr and opened a
            # new conversation with rc=0. Emit an error but no thread.started
            # so the runner fails the turn and keeps the requested id.
            self._stale = True
            return [
                {
                    "type": "error",
                    "message": (
                        f'conversation "{self._expected_id}" not found; '
                        f'agy started new conversation "{conv}"'
                    ),
                }
            ]
        self._thread_seen = True
        return [{"type": "thread.started", "thread_id": conv}]

    def _on_step(self, step: dict[str, Any]) -> list[dict[str, Any]]:
        state = str(step.get("state") or "")
        step_type = str(step.get("step_type") or "")
        if step_type == "user_input":
            # First step of every turn: the canonical turn boundary marker.
            if self._turn_started:
                return [_NOOP]
            self._turn_started = True
            return [{"type": "turn.started"}]
        item_type = _TEXT_STEPS.get(step_type)
        if item_type is not None:
            return self._text_step(step, item_type, state)
        if step_type in _TOOL_STEPS:
            return self._tool_step(step, state)
        # system_message and anything unrecognised-but-parseable: acknowledged.
        return [_NOOP]

    def _text_step(self, step: dict[str, Any], item_type: str, state: str) -> list[dict[str, Any]]:
        """Buffer ``text_delta`` per step; emit one item.completed at DONE.

        ``text_delta`` fragments arrive across ACTIVE and DONE updates of the
        same ``step_index``; concatenated they equal that step's share of the
        final response.
        """
        key = (item_type, step.get("step_index"))
        delta = step.get("text_delta")
        if isinstance(delta, str) and delta:
            self._text_buf.setdefault(key, []).append(delta)
        if state not in ("DONE", "FAILED"):
            return [_NOOP]
        text = "".join(self._text_buf.pop(key, []))
        if not text:
            return [_NOOP]
        if item_type == "agent_message":
            self._emitted_agent_message = True
        return [
            {
                "type": "item.completed",
                "item": {"id": self._next_item_id(), "type": item_type, "text": text},
            }
        ]

    def _tool_step(self, step: dict[str, Any], state: str) -> list[dict[str, Any]]:
        idx = step.get("step_index")
        tool_info = step.get("tool_info")
        if not isinstance(tool_info, dict):
            tool_info = {}
        tool_name = str(step.get("tool_name") or tool_info.get("name") or "")
        params = tool_info.get("parameters")
        if not isinstance(params, dict):
            params = {}
        if state == "ACTIVE":
            item_id = self._open_tools.get(idx) or self._next_item_id()
            self._open_tools[idx] = item_id
            item = self._tool_item(item_id, tool_name, params, tool_info, status="in_progress")
            return [{"type": "item.started", "item": item}]
        if state in ("DONE", "FAILED"):
            item_id = self._open_tools.pop(idx, None) or self._next_item_id()
            status = "completed" if state == "DONE" else "failed"
            item = self._tool_item(item_id, tool_name, params, tool_info, status=status)
            return [{"type": "item.completed", "item": item}]
        return [_NOOP]

    @staticmethod
    def _tool_item(
        item_id: str,
        tool_name: str,
        params: dict[str, Any],
        tool_info: dict[str, Any],
        *,
        status: str,
    ) -> dict[str, Any]:
        if tool_name in _FILE_TOOLS:
            path = (
                params.get("TargetFile")
                or params.get("target_file")
                or params.get("file_path")
                or params.get("path")
                or ""
            )
            return {
                "id": item_id,
                "type": "file_change",
                "changes": [{"path": str(path), "kind": "update"}],
                "status": status,
            }
        command = params.get("CommandLine") or params.get("command") or ""
        if not command:
            command = tool_name
            if params:
                command = f"{tool_name} {json.dumps(params, ensure_ascii=False)}"
        return {
            "id": item_id,
            "type": "command_execution",
            "command": str(command),
            "aggregated_output": (
                "" if status == "in_progress" else str(tool_info.get("output") or "")
            ),
            "status": status,
            "exit_code": 0 if status == "completed" else None,
        }

    def _on_result(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        conv = result.get("conversation_id")
        if (
            isinstance(conv, str)
            and conv
            and not self._thread_seen
            and not self._stale
            and (self._expected_id is None or conv == self._expected_id)
        ):
            # Defensive fallback: session marker only seen on the result line.
            self._thread_seen = True
            events.append({"type": "thread.started", "thread_id": conv})
        events.extend(self._flush_text())
        if self._stale:
            events.append(
                {
                    "type": "turn.failed",
                    "error": {
                        "message": (f'resume failed: conversation "{self._expected_id}" not found')
                    },
                }
            )
            return events
        if str(result.get("status") or "") == "SUCCESS":
            response = result.get("response")
            if isinstance(response, str) and response and not self._emitted_agent_message:
                self._emitted_agent_message = True
                events.append(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": self._next_item_id(),
                            "type": "agent_message",
                            "text": response,
                        },
                    }
                )
            events.append({"type": "turn.completed", "usage": _map_usage(result.get("usage"))})
            return events
        events.append({"type": "turn.failed", "error": {"message": _error_message(result)}})
        return events

    def _flush_text(self) -> list[dict[str, Any]]:
        """Emit buffered text for steps that ended without a DONE update."""
        events: list[dict[str, Any]] = []
        for (item_type, _idx), parts in self._text_buf.items():
            text = "".join(parts)
            if not text:
                continue
            if item_type == "agent_message":
                self._emitted_agent_message = True
            events.append(
                {
                    "type": "item.completed",
                    "item": {"id": self._next_item_id(), "type": item_type, "text": text},
                }
            )
        self._text_buf.clear()
        return events
