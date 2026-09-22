"""OpenCode CLI adapter (SOR-96).

Drives ``opencode run <PROMPT> --format json -m <provider/model> --dir
$SBX_WORK --auto`` per the OpenCode CLI reference (``run [message..]``,
``--format json`` = raw JSON events, ``--auto`` auto-approves permission
asks in non-interactive mode, ``-s/--session <id>`` continues a session).
One process per turn, stdin DEVNULL. ``OPENCODE_BIN`` overrides the CLI
binary (tests point it at a replay helper or ``tests/fakes/
fake_opencode.py``).

Native ``--format json`` line kinds handled (real stream shape; the WP0
hand-written fixtures under ``tests/fixtures/events/opencode/`` use the
same vocabulary):

- Every line carries ``sessionID`` (top level and ``part.sessionID``):
  the first sighting is the session marker -> ``thread.started``
  (events.md translation rule 1).
- ``step_start`` -> ``turn.started`` once. Multi-step tool loops emit
  one ``step_start`` per step; later ones are NOOP.
- ``reasoning`` / ``text`` -> part snapshots keyed by ``part.id``
  (``part.text`` is cumulative, so the latest line wins — never
  concatenated). Buffered parts flush to ``item.completed`` items
  (``reasoning`` / ``agent_message``) when a ``tool_use``,
  ``step_finish`` or ``error`` boundary arrives.
- ``tool_use`` -> ``item.started`` on first sight of ``part.callID``
  (``state.status`` ``pending``/``running``); ``item.completed`` when
  ``status`` is ``completed`` or ``error``. A first-sight terminal event
  emits started+completed together (the WP0 fixture shape). File-write
  tools (``edit``/``write``/``patch``...) -> ``file_change``;
  ``bash`` and the rest -> ``command_execution`` with
  ``state.input.command`` / ``state.output`` / ``state.metadata.exit``.
- ``step_finish`` -> ``part.reason == "tool-calls"`` is an intermediate
  step boundary (flush buffered parts, NOOP). Any other reason
  (``stop`` or absent) closes the turn: flush parts, then
  ``turn.completed{usage}``. ``part.tokens`` across all step_finish
  parts accumulate into the canonical five usage fields
  (``input``/``output``/``reasoning``/``cache.read``/``cache.write``).
- ``error`` -> ``error{message}`` + ``turn.failed`` (fatal: auth
  failure, unknown model, ...). Both ``{error:{data:{message}}}`` (real
  ``APIError``) and flat ``{message}``/``{error:{message}}`` forms are
  read.
- Unknown-but-parseable kinds -> NOOP (SOR-80 forward compatibility).
  ``[]`` is reserved for lines with no JSON object, which the runner
  counts as bad JSON (events.md rule 5).

Stale ``--session`` id: ``resume_argv`` records the requested id; a
mismatching ``sessionID`` suppresses ``thread.started`` and fails the
turn (agy/grok precedent) instead of forking the session. The real CLI
may also drop the terminal ``step_finish`` (anomalyco/opencode run.ts
emits it only while the loop is still attached); the runner's
``sbx.turn_finished`` then remains the authoritative terminal record.

``health_from`` scans stderr first, then the native ``error`` messages
seen on the stream: under ``--format json`` the real CLI emits fatal
errors (401/429/...) only as stdout ``error`` events — ``UI.error`` is
skipped once ``emit`` succeeds — so stderr alone cannot classify them.
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
from runtime.runner.workspace import agent_workdir, load_session, work_root

Health = Literal["ok", "auth_invalid", "rate_limited", "unknown"]

# filesystem.md: opencode credential lives at the XDG data path
# ``~/.local/share/opencode/auth.json`` (provider keys / OAuth blobs).
OPENCODE_AUTH_REL = ".local/share/opencode/auth.json"

_AUTH_NEEDLES = (
    "incorrect api key",
    "invalid api key",
    "unauthorized",
    "unauthenticated",
    "authentication",
    "no credentials",
    "not signed in",
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
    "overloaded",
)

_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

# OpenCode tool names that produce file_change items; ``bash`` and the
# rest produce command_execution.
_FILE_TOOLS = {
    "edit",
    "write",
    "patch",
    "apply_patch",
    "multiedit",
    "multi_edit",
    "str_replace",
    "str_replace_editor",
    "write_to_file",
    "replace_file_content",
}

_TERMINAL_TOOL_STATUSES = ("completed", "error")
_STEP_CONTINUE = "tool-calls"

_NOOP = {"type": NOOP_EVENT_TYPE}


def opencode_bin_tokens() -> list[str]:
    raw = os.environ.get("OPENCODE_BIN", "opencode")
    tokens = shlex.split(raw)
    if not tokens:
        tokens = ["opencode"]
    if len(tokens) == 1 and tokens[0].endswith(".py"):
        return [sys.executable, tokens[0]]
    return tokens


def _session_model() -> str:
    """Model recorded by ``runner init`` in session.json, for resume argv."""
    raw = load_session(work_root()).get("model")
    return raw if isinstance(raw, str) else ""


def _empty_usage() -> dict[str, int]:
    return {field: 0 for field in _USAGE_FIELDS}


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _add_tokens(acc: dict[str, int], tokens: Any) -> None:
    """Accumulate one ``step_finish`` ``part.tokens`` block into canonical usage."""
    if not isinstance(tokens, dict):
        return
    acc["input_tokens"] += _as_int(tokens.get("input"))
    acc["output_tokens"] += _as_int(tokens.get("output"))
    acc["reasoning_output_tokens"] += _as_int(tokens.get("reasoning"))
    cache = tokens.get("cache")
    if isinstance(cache, dict):
        acc["cached_input_tokens"] += _as_int(cache.get("read"))
        acc["cache_write_input_tokens"] += _as_int(cache.get("write"))


def _error_message(obj: dict[str, Any]) -> str:
    """``{error:{data:{message}}}`` (real APIError) or flat/nested forms."""
    err = obj.get("error")
    if isinstance(err, dict):
        data = err.get("data")
        if isinstance(data, dict):
            msg = data.get("message")
            if isinstance(msg, str) and msg:
                return msg
        msg = err.get("message")
        if isinstance(msg, str) and msg:
            return msg
        return json.dumps(err, ensure_ascii=False)
    if isinstance(err, str) and err:
        return err
    msg = obj.get("message")
    if isinstance(msg, str) and msg:
        return msg
    return "opencode turn failed"


class OpencodeAdapter:
    """OpenCode (``opencode run --format json``) NDJSON -> canonical events."""

    provider = "opencode"
    credential_files: tuple[str, ...] = (OPENCODE_AUTH_REL,)

    def __init__(self) -> None:
        self._item_seq = 0
        self._expected_id: str | None = None
        self._stale = False
        self._thread_seen = False
        self._turn_started = False
        self._turn_terminal = False
        # Buffered text/reasoning part snapshots: part.id -> (kind, text).
        # ``part.text`` is cumulative, so the latest snapshot replaces.
        self._part_order: list[str] = []
        self._parts: dict[str, tuple[str, str]] = {}
        # callID -> {"tool", "input"} for open calls; terminal callIDs.
        self._open_tools: dict[str, dict[str, Any]] = {}
        self._done_tools: set[str] = set()
        self._usage_acc = _empty_usage()
        # Native ``error``-line messages, for health_from's stream fallback.
        self._stream_errors: list[str] = []

    def prepare_home(self, home: Path, model: str) -> None:
        """Create ``~/.local/share/opencode`` (700); keep ``auth.json`` at 600.

        The credential blob restores ``.local/share/opencode/auth.json``
        before this runs; ``prepare_home`` never writes its contents. The
        CLI self-populates the rest of the data dir (project storage,
        session DB) under the same root — ``$HOME`` only needs to be
        writable. Approval bypass is argv-level (``--auto``) and ``model``
        travels on argv, so no config file is written.
        """
        data_dir = home / ".local" / "share" / "opencode"
        data_dir.mkdir(parents=True, exist_ok=True)
        data_dir.chmod(0o700)
        auth = data_dir / "auth.json"
        if auth.is_file():
            auth.chmod(0o600)

    def first_turn_argv(self, prompt: str, model: str) -> list[str]:
        argv = [*opencode_bin_tokens(), "run", prompt, "--format", "json"]
        if model:
            argv += ["-m", model]
        argv += ["--dir", str(agent_workdir()), "--auto"]
        return argv

    def resume_argv(self, prompt: str, native_session_id: str) -> list[str]:
        self._expected_id = native_session_id
        argv = [
            *opencode_bin_tokens(),
            "run",
            prompt,
            "--format",
            "json",
            "--session",
            native_session_id,
        ]
        model = _session_model()
        if model:
            argv += ["-m", model]
        argv += ["--dir", str(agent_workdir()), "--auto"]
        return argv

    def translate(self, raw_line: str) -> list[dict[str, Any]]:
        obj, bad = parse_event_line(raw_line)
        if bad or obj is None:
            return []
        events = self._thread_or_stale(self._session_marker(obj))
        if self._stale:
            kind = obj.get("type")
            if kind == "error":
                message = _error_message(obj)
                events.append({"type": "error", "message": message})
                events.extend(self._stale_failed())
            elif kind == "step_finish":
                events.extend(self._on_step_finish(obj))
            return events or [_NOOP]
        kind = obj.get("type")
        if kind == "step_start":
            events += self._ensure_turn_started()
        elif kind in ("reasoning", "text"):
            events += self._on_part(obj, "reasoning" if kind == "reasoning" else "agent_message")
        elif kind == "tool_use":
            events += self._on_tool_use(obj)
        elif kind == "step_finish":
            events += self._on_step_finish(obj)
        elif kind == "error":
            events += self._on_error(obj)
        # Forward compatibility: a parseable object of an unknown kind is
        # acknowledged as NOOP, never a bad line. ``[]`` is reserved for
        # lines with no JSON object (unparseable / non-object), which the
        # runner counts as bad JSON (events.md rule 5).
        return events or [_NOOP]

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
        stream = " ".join(self._stream_errors).lower()
        if any(needle in stream for needle in _AUTH_NEEDLES):
            return "auth_invalid"
        if any(needle in stream for needle in _RATE_NEEDLES):
            return "rate_limited"
        return "unknown"

    # -- internals ---------------------------------------------------------

    def _next_item_id(self) -> str:
        item_id = f"item_{self._item_seq}"
        self._item_seq += 1
        return item_id

    def _ensure_turn_started(self) -> list[dict[str, Any]]:
        if self._turn_started:
            return []
        self._turn_started = True
        return [{"type": "turn.started"}]

    def _session_marker(self, obj: dict[str, Any]) -> str | None:
        sid = obj.get("sessionID")
        if isinstance(sid, str) and sid:
            return sid
        part = obj.get("part")
        if isinstance(part, dict):
            sid = part.get("sessionID")
            if isinstance(sid, str) and sid:
                return sid
        return None

    def _thread_or_stale(self, sid: str | None) -> list[dict[str, Any]]:
        """Emit thread.started for ``sid`` unless it contradicts a requested
        ``--session`` id (defensive; a real stale id errors on stderr)."""
        if not sid or self._thread_seen or self._stale:
            return []
        if self._expected_id is not None and sid != self._expected_id:
            self._stale = True
            return [
                {
                    "type": "error",
                    "message": (
                        f'session "{self._expected_id}" not found; '
                        f'opencode reported session "{sid}"'
                    ),
                }
            ]
        self._thread_seen = True
        return [{"type": "thread.started", "thread_id": sid}]

    def _stale_failed(self) -> list[dict[str, Any]]:
        if self._turn_terminal:
            return []
        self._turn_terminal = True
        return [
            {
                "type": "turn.failed",
                "error": {"message": f'resume failed: session "{self._expected_id}" not found'},
            }
        ]

    def _on_part(self, obj: dict[str, Any], item_type: str) -> list[dict[str, Any]]:
        part = obj.get("part")
        if not isinstance(part, dict):
            return [_NOOP]
        part_id = part.get("id")
        text = part.get("text")
        if not isinstance(text, str) or not text:
            return [_NOOP]
        key = str(part_id) if part_id is not None else f"{item_type}:{len(self._part_order)}"
        if key not in self._parts:
            self._part_order.append(key)
        self._parts[key] = (item_type, text)  # cumulative snapshot: replace
        return self._ensure_turn_started() or [_NOOP]

    def _flush_parts(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for key in self._part_order:
            item_type, text = self._parts[key]
            events.append(
                {
                    "type": "item.completed",
                    "item": {"id": self._next_item_id(), "type": item_type, "text": text},
                }
            )
        self._part_order = []
        self._parts = {}
        return events

    def _tool_state(self, obj: dict[str, Any]) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
        part = obj.get("part")
        if not isinstance(part, dict):
            part = {}
        state = part.get("state")
        if not isinstance(state, dict):
            state = {}
        call_id = part.get("callID") or part.get("id")
        tool = str(part.get("tool") or state.get("tool") or "")
        raw_input = state.get("input")
        if not isinstance(raw_input, dict):
            raw_input = {}
        return str(call_id or ""), tool, state, raw_input

    def _tool_item(
        self,
        item_id: str,
        tool: str,
        raw_input: dict[str, Any],
        state: dict[str, Any],
        *,
        status: str,
    ) -> dict[str, Any]:
        if tool in _FILE_TOOLS:
            path = (
                raw_input.get("filePath")
                or raw_input.get("file_path")
                or raw_input.get("path")
                or raw_input.get("TargetFile")
                or ""
            )
            return {
                "id": item_id,
                "type": "file_change",
                "changes": [{"path": str(path), "kind": "update"}],
                "status": status,
            }
        command = raw_input.get("command") or raw_input.get("CommandLine")
        if not command:
            title = state.get("title")
            command = title if isinstance(title, str) and title else tool
        if not command and raw_input:
            command = json.dumps(raw_input, ensure_ascii=False)
        output = state.get("output")
        metadata = state.get("metadata")
        exit_code = metadata.get("exit") if isinstance(metadata, dict) else None
        return {
            "id": item_id,
            "type": "command_execution",
            "command": str(command or ""),
            "aggregated_output": output if isinstance(output, str) else "",
            "status": status,
            "exit_code": exit_code if isinstance(exit_code, int) else None,
        }

    def _on_tool_use(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        call_id, tool, state, raw_input = self._tool_state(obj)
        status = str(state.get("status") or "")
        item_id = call_id or self._next_item_id()
        if item_id in self._done_tools:
            return [_NOOP]
        opened = item_id in self._open_tools
        if status not in _TERMINAL_TOOL_STATUSES:
            if opened:
                return [_NOOP]  # repeated pending/running update
            events = self._ensure_turn_started() + self._flush_parts()
            self._open_tools[item_id] = {"tool": tool, "input": raw_input}
            events.append(
                {
                    "type": "item.started",
                    "item": self._tool_item(item_id, tool, raw_input, state, status="in_progress"),
                }
            )
            return events
        events = self._ensure_turn_started() + self._flush_parts()
        self._done_tools.add(item_id)
        self._open_tools.pop(item_id, None)
        if not opened:
            # First sight is already terminal (WP0 fixture shape): emit
            # started+completed together per the item contract.
            events.append(
                {
                    "type": "item.started",
                    "item": self._tool_item(item_id, tool, raw_input, state, status="in_progress"),
                }
            )
        events.append(
            {
                "type": "item.completed",
                "item": self._tool_item(
                    item_id,
                    tool,
                    raw_input,
                    state,
                    status="completed" if status == "completed" else "failed",
                ),
            }
        )
        return events

    def _on_step_finish(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        part = obj.get("part")
        if not isinstance(part, dict):
            part = {}
        _add_tokens(self._usage_acc, part.get("tokens"))
        events = self._flush_parts()
        reason = str(part.get("reason") or "")
        if reason == _STEP_CONTINUE or self._turn_terminal:
            # Intermediate tool-loop boundary, or a duplicate terminal
            # part: accumulate usage, never re-close the turn.
            return events or [_NOOP]
        self._turn_terminal = True
        if self._stale:
            events.append(
                {
                    "type": "turn.failed",
                    "error": {
                        "message": (f'resume failed: session "{self._expected_id}" not found')
                    },
                }
            )
        elif reason in ("", "stop"):
            events.append({"type": "turn.completed", "usage": dict(self._usage_acc)})
        else:
            events.append(
                {
                    "type": "turn.failed",
                    "error": {"message": f"opencode turn ended with reason {reason}"},
                }
            )
        return events

    def _on_error(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        events = self._ensure_turn_started() + self._flush_parts()
        message = _error_message(obj)
        self._stream_errors.append(message)
        events.append({"type": "error", "message": message})
        if not self._turn_terminal:
            self._turn_terminal = True
            events.append({"type": "turn.failed", "error": {"message": message}})
        return events
