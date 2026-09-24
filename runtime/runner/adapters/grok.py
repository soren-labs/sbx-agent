"""Grok Build CLI adapter (SOR-62).

Drives ``grok -p <PROMPT> --output-format streaming-json --model <M>
--permission-mode bypassPermissions`` per the verified SOR-60 spike
(``spike/p2/GROK_SPIKE.md`` + Modal clean-room PASS in
``spike/p2/out/modal_grok.json``): one process per turn, ``--resume <id>``
for follow-ups, stdin DEVNULL. Note the real CLI's ``-p`` **takes the
prompt as its value** and ``-s/--session-id`` names a *new* session —
resume is ``-r/--resume <id>`` (the WP0 fake's argv differs; it is
tolerated, not matched).

``GROK_BIN`` overrides the CLI binary (tests point it at a replay helper
or ``tests/fakes/fake_grok.py``).

Native ``streaming-json`` line kinds handled (real 1.0.24 shape plus the
WP0 hand-written fixture shape):

- ``available_commands`` -> NOOP (repeats several times per run).
- ``plan`` -> NOOP (real 1.0.24 emits
  ``{"type":"plan","entries":[{"content","priority","status"}]}``
  progress blocks mid-turn; non-terminal, no canonical event).
- ``thought`` / ``text`` -> ``data`` deltas (real) or whole ``text``
  strings (WP0 fake); concatenated per contiguous segment into
  ``reasoning`` / ``agent_message`` ``item.completed`` events.
- ``usage`` (step-level, per model call) -> NOOP. Only the terminal
  ``end.usage`` feeds ``turn.completed`` (spike: step usage is a per-call
  figure; ``signature`` is dropped and redacted via ``_SECRET_KEYS``).
- ``tool_call`` -> ``item.started``. ``kind=="write"`` or a file-tool
  name (``write``/``search_replace``/...) -> ``file_change``;
  ``kind=="execute"`` / other tools -> ``command_execution``. Real shape
  keys: ``toolCallId``/``toolName``/``rawInput``; WP0 fake:
  ``name``/``arguments``.
- ``tool_call_update`` -> ``item.completed`` **only** when
  ``status=="completed"`` or ``"failed"``; ``status:null`` and
  ``in_progress`` updates are tolerated as NOOP (the real CLI emits a
  ``status:null`` update first — see ``fixtures/grok/success.jsonl``).
  Output comes from ``rawOutput.output_for_prompt`` (Bash) or
  ``rawOutput.EditsApplied.tool_output_for_prompt`` (SearchReplace);
  ``rawOutput.output`` is a byte array and is not used.
- ``tool_result`` (WP0 fake) -> ``item.completed`` closing the open call
  paired by ``name``.
- ``init`` (WP0 fake) -> ``thread.started{thread_id: sessionId}``. Real
  streaming-json has **no** init line; ``end.sessionId`` is the first
  session marker (events.md translation rule 1).
- ``end`` -> flush pending text, emit ``thread.started`` when the id was
  not seen earlier, then ``turn.completed{usage}`` (``stopReason``
  ``end_turn``/``stop``) or ``turn.failed``.
- ``error`` -> ``error{message}`` + ``turn.failed`` (the line is fatal:
  not-signed-in, unknown model, ...). Both flat ``{message}`` (real) and
  nested ``{error:{message}}`` (WP0) forms are read.

Stale ``--resume`` id: the real CLI exits rc=1 with **empty stdout** and a
stderr ``Failed to restore session from remote: ... 404 Not Found`` — no
stream signal — so the runner's rc path fails the turn. As a defensive
guard (agy precedent) ``resume_argv`` records the requested id and a
mismatching ``sessionId`` suppresses ``thread.started`` and fails the
turn instead of forking the session.

Defensive ``streaming-messages-json`` coverage (the spike's recommended
fallback format): ``system.init`` -> ``thread.started``; ``assistant``
content blocks -> ``reasoning``/``agent_message``/``item.started``;
``user`` ``tool_result`` blocks -> ``item.completed``; ``result`` ->
``turn.completed``/``turn.failed``; ``stream_event`` -> NOOP.

Forward compatibility (SOR-80): any parseable JSON object of an unknown
kind also yields NOOP — ``translate`` only returns ``[]`` when the line
carries no JSON object (unparseable text or non-object JSON), which is
what ``runner turn`` counts as a bad line (events.md rule 5).
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
from runtime.runner.effort import native_effort
from runtime.runner.events import parse_event_line
from runtime.runner.workspace import load_session, work_root

Health = Literal["ok", "auth_invalid", "rate_limited", "unknown"]

GROK_AUTH_REL = ".grok/auth.json"

# GROK_SPIKE.md §auth-invalid signature: rc=1 fast with
# "Not signed in. ..." on both stdout (error line) and stderr (Error: ...).
_AUTH_NEEDLES = (
    "not signed in",
    "not authenticated",
    "unauthorized",
    "unauthenticated",
    "invalid api key",
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

# native usage key -> canonical usage key (events.md usage_mapping + the
# real end.usage field names; total_tokens/modelUsage/signature have no
# canonical field and are ignored).
_USAGE_MAP = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cached_input_tokens",
    "cache_creation_input_tokens": "cache_write_input_tokens",
    "reasoning_tokens": "reasoning_output_tokens",
    # WP0 fake / contract aliases.
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

# Native tool names that produce file_change items; ``kind=="write"``
# forces it too. Everything else produces command_execution.
_FILE_TOOLS = {
    "write",
    "search_replace",
    "edit",
    "multi_edit",
    "str_replace",
    "apply_patch",
    "notebook_edit",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
}

_NOOP = {"type": NOOP_EVENT_TYPE}


def grok_bin_tokens() -> list[str]:
    raw = os.environ.get("GROK_BIN", "grok")
    tokens = shlex.split(raw)
    if not tokens:
        tokens = ["grok"]
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


def _error_message(obj: dict[str, Any]) -> str:
    """Flat ``{message}`` (real) or nested ``{error:{message}}`` (WP0)."""
    msg = obj.get("message")
    if isinstance(msg, str) and msg:
        return msg
    err = obj.get("error")
    if isinstance(err, dict):
        inner = err.get("message")
        if isinstance(inner, str) and inner:
            return inner
        return json.dumps(err, ensure_ascii=False)
    if isinstance(err, str) and err:
        return err
    return "grok turn failed"


class GrokAdapter:
    """Grok Build (``grok``) adapter: streaming-json NDJSON -> canonical events."""

    provider = "grok"
    credential_files: tuple[str, ...] = (GROK_AUTH_REL,)

    def __init__(self) -> None:
        self._item_seq = 0
        self._expected_id: str | None = None
        self._stale = False
        self._thread_seen = False
        self._turn_started = False
        self._emitted_agent_message = False
        # Contiguous text segment being buffered: "reasoning" | "agent_message".
        self._seg_type: str | None = None
        self._seg_buf: list[str] = []
        # Open tool calls: item_id -> {name, kind, raw_input}; plus a
        # name -> item_id index for the WP0 fake's tool_result pairing.
        self._open_tools: dict[str, dict[str, Any]] = {}
        self._open_by_name: dict[str, str] = {}

    def prepare_home(self, home: Path, model: str) -> None:
        """Create ``~/.grok`` (700); keep ``auth.json`` at 600.

        The credential blob restores ``.grok/auth.json`` before this runs;
        ``prepare_home`` never writes its contents. The CLI self-populates
        the rest of ``~/.grok`` on first run (bundled/, sessions/,
        models_cache.json) — ``$HOME`` only needs to be writable. Approval
        bypass is argv-level (``--permission-mode bypassPermissions``) and
        ``model`` travels on argv, so no config file is written.
        """
        grok_dir = home / ".grok"
        grok_dir.mkdir(parents=True, exist_ok=True)
        grok_dir.chmod(0o700)
        auth = grok_dir / "auth.json"
        if auth.is_file():
            auth.chmod(0o600)

    def first_turn_argv(self, prompt: str, model: str) -> list[str]:
        argv = [*grok_bin_tokens(), "-p", prompt, "--output-format", "streaming-json"]
        if model:
            argv += ["--model", model]
        effort = _session_effort()
        if effort:
            argv += ["--effort", native_effort("grok", effort)]
        argv += ["--permission-mode", "bypassPermissions"]
        return argv

    def resume_argv(self, prompt: str, native_session_id: str) -> list[str]:
        self._expected_id = native_session_id
        argv = [
            *grok_bin_tokens(),
            "-p",
            prompt,
            "--resume",
            native_session_id,
            "--output-format",
            "streaming-json",
        ]
        model = _session_model()
        if model:
            argv += ["--model", model]
        effort = _session_effort()
        if effort:
            # Resume turns inherit the declared effort natively (SOR-179).
            argv += ["--effort", native_effort("grok", effort)]
        argv += ["--permission-mode", "bypassPermissions"]
        return argv

    def translate(self, raw_line: str) -> list[dict[str, Any]]:
        obj, bad = parse_event_line(raw_line)
        if bad or obj is None:
            return []
        kind = obj.get("type")
        if kind in ("available_commands", "plan"):
            # ``plan`` is the real 1.0.24 progress block
            # (``entries:[{content, priority, status}]``): non-terminal
            # bookkeeping with no canonical event -> NOOP.
            return [_NOOP]
        if kind in ("thought", "text"):
            return self._on_text_delta(obj, "reasoning" if kind == "thought" else "agent_message")
        if kind == "usage":
            return [_NOOP]
        if kind == "tool_call":
            return self._on_tool_call(obj)
        if kind == "tool_call_update":
            return self._on_tool_update(obj)
        if kind == "tool_result":
            return self._on_tool_result(obj)
        if kind in ("init", "system.init"):
            return self._on_init(obj)
        if kind == "end":
            return self._on_end(obj)
        if kind == "result":
            return self._on_result(obj)
        if kind == "assistant":
            return self._on_assistant(obj)
        if kind == "user":
            return self._on_user(obj)
        if kind == "stream_event":
            return [_NOOP]
        if kind == "error":
            return self._on_error(obj)
        # Forward compatibility: a parseable object of an unknown kind is
        # acknowledged as NOOP, never a bad line. ``[]`` is reserved for
        # lines with no JSON object (unparseable / non-object), which the
        # runner counts as bad JSON (events.md rule 5).
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

    def _ensure_turn_started(self) -> list[dict[str, Any]]:
        if self._turn_started:
            return []
        self._turn_started = True
        return [{"type": "turn.started"}]

    def _on_text_delta(self, obj: dict[str, Any], item_type: str) -> list[dict[str, Any]]:
        data = obj.get("data")
        if not isinstance(data, str) or not data:
            data = obj.get("text")  # WP0 fake field name
        if not isinstance(data, str) or not data:
            return [_NOOP]
        events = self._ensure_turn_started()
        if self._seg_type is not None and self._seg_type != item_type:
            events.extend(self._flush_segment())
        self._seg_type = item_type
        self._seg_buf.append(data)
        return events or [_NOOP]

    def _flush_segment(self) -> list[dict[str, Any]]:
        text = "".join(self._seg_buf)
        self._seg_buf = []
        seg_type = self._seg_type
        self._seg_type = None
        if not text or seg_type is None:
            return []
        if seg_type == "agent_message":
            self._emitted_agent_message = True
        return [
            {
                "type": "item.completed",
                "item": {"id": self._next_item_id(), "type": seg_type, "text": text},
            }
        ]

    def _session_marker(self, obj: dict[str, Any]) -> str | None:
        sid = obj.get("sessionId") or obj.get("session_id")
        if isinstance(sid, str) and sid:
            return sid
        return None

    def _thread_or_stale(self, sid: str | None) -> list[dict[str, Any]]:
        """Emit thread.started for ``sid`` unless it contradicts a requested
        ``--resume`` id (defensive; the real CLI fails rc=1 instead)."""
        if not sid or self._thread_seen:
            return []
        if self._expected_id is not None and sid != self._expected_id:
            self._stale = True
            return [
                {
                    "type": "error",
                    "message": (
                        f'session "{self._expected_id}" not found; grok reported session "{sid}"'
                    ),
                }
            ]
        self._thread_seen = True
        return [{"type": "thread.started", "thread_id": sid}]

    def _on_init(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        events = self._thread_or_stale(self._session_marker(obj))
        if self._stale:
            return events
        events += self._ensure_turn_started()
        return events or [_NOOP]

    def _tool_identity(self, obj: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        name = str(obj.get("toolName") or obj.get("name") or obj.get("title") or "")
        kind = str(obj.get("kind") or "")
        raw_input = obj.get("rawInput")
        if not isinstance(raw_input, dict):
            raw_input = obj.get("arguments")
        if not isinstance(raw_input, dict):
            raw_input = {}
        return name, kind, raw_input

    def _is_file_tool(self, name: str, kind: str) -> bool:
        return name in _FILE_TOOLS or kind == "write"

    def _file_path(self, raw_input: dict[str, Any], obj: dict[str, Any]) -> str:
        path = raw_input.get("file_path") or raw_input.get("path") or raw_input.get("TargetFile")
        if not path:
            locations = obj.get("locations")
            if isinstance(locations, list):
                for loc in locations:
                    if isinstance(loc, dict) and loc.get("path"):
                        path = loc["path"]
                        break
        if not path:
            content = obj.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "diff" and block.get("path"):
                        path = block["path"]
                        break
        return str(path or "")

    def _tool_item(
        self,
        item_id: str,
        name: str,
        kind: str,
        raw_input: dict[str, Any],
        obj: dict[str, Any],
        *,
        status: str,
        output: str = "",
        exit_code: int | None = None,
    ) -> dict[str, Any]:
        if self._is_file_tool(name, kind):
            return {
                "id": item_id,
                "type": "file_change",
                "changes": [{"path": self._file_path(raw_input, obj), "kind": "update"}],
                "status": status,
            }
        command = raw_input.get("command") or raw_input.get("CommandLine") or name
        if not command and raw_input:
            command = json.dumps(raw_input, ensure_ascii=False)
        return {
            "id": item_id,
            "type": "command_execution",
            "command": str(command or ""),
            "aggregated_output": output,
            "status": status,
            "exit_code": exit_code,
        }

    def _on_tool_call(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        events = self._ensure_turn_started() + self._flush_segment()
        call_id = obj.get("toolCallId")
        if isinstance(call_id, str) and call_id:
            item_id = call_id
        else:
            item_id = self._next_item_id()
        name, kind, raw_input = self._tool_identity(obj)
        self._open_tools[item_id] = {"name": name, "kind": kind, "raw_input": raw_input}
        if name:
            self._open_by_name[name] = item_id
        events.append(
            {
                "type": "item.started",
                "item": self._tool_item(item_id, name, kind, raw_input, obj, status="in_progress"),
            }
        )
        return events

    @staticmethod
    def _tool_output(raw_output: Any) -> tuple[str, int | None]:
        """Best-effort (text, exit_code) out of a native rawOutput object."""
        if not isinstance(raw_output, dict):
            return "", None
        output = raw_output.get("output_for_prompt")
        if not isinstance(output, str) or not output:
            edits = raw_output.get("EditsApplied")
            if isinstance(edits, dict):
                output = edits.get("tool_output_for_prompt")
        if not isinstance(output, str):
            output = ""
        exit_code = raw_output.get("exit_code")
        return output, exit_code if isinstance(exit_code, int) else None

    def _on_tool_update(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        status = obj.get("status")
        # Real CLI emits status:null first, then in_progress; only a
        # terminal status produces item.completed (spike handoff).
        if status not in ("completed", "failed"):
            return [_NOOP]
        events = self._ensure_turn_started() + self._flush_segment()
        call_id = obj.get("toolCallId")
        item_id = call_id if isinstance(call_id, str) and call_id else self._next_item_id()
        open_info = self._open_tools.pop(item_id, {})
        name, kind, raw_input = self._tool_identity(obj)
        name = name or str(open_info.get("name") or "")
        kind = kind or str(open_info.get("kind") or "")
        if not raw_input:
            prior = open_info.get("raw_input")
            raw_input = prior if isinstance(prior, dict) else {}
        output, exit_code = self._tool_output(obj.get("rawOutput"))
        item_status = "completed" if status == "completed" else "failed"
        if exit_code is None and item_status == "completed":
            exit_code = 0
        events.append(
            {
                "type": "item.completed",
                "item": self._tool_item(
                    item_id,
                    name,
                    kind,
                    raw_input,
                    obj,
                    status=item_status,
                    output=output,
                    exit_code=exit_code,
                ),
            }
        )
        return events

    def _on_tool_result(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        """WP0 fake shape: ``{name, exit_code, output}`` closes the call."""
        events = self._ensure_turn_started() + self._flush_segment()
        name = str(obj.get("name") or obj.get("toolName") or "")
        item_id = self._open_by_name.pop(name, None) or self._next_item_id()
        open_info = self._open_tools.pop(item_id, {})
        kind = str(open_info.get("kind") or "")
        prior = open_info.get("raw_input")
        raw_input = prior if isinstance(prior, dict) else {}
        exit_code = obj.get("exit_code")
        output = obj.get("output")
        events.append(
            {
                "type": "item.completed",
                "item": self._tool_item(
                    item_id,
                    name or str(open_info.get("name") or ""),
                    kind,
                    raw_input,
                    obj,
                    status="completed",
                    output=output if isinstance(output, str) else "",
                    exit_code=exit_code if isinstance(exit_code, int) else 0,
                ),
            }
        )
        return events

    def _on_end(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        events = self._ensure_turn_started()
        events.extend(self._thread_or_stale(self._session_marker(obj)))
        events.extend(self._flush_segment())
        if self._stale:
            events.append(
                {
                    "type": "turn.failed",
                    "error": {"message": f'resume failed: session "{self._expected_id}" not found'},
                }
            )
            return events
        stop = str(obj.get("stopReason") or obj.get("stop_reason") or "")
        if stop in ("", "end_turn", "stop"):
            events.append({"type": "turn.completed", "usage": _map_usage(obj.get("usage"))})
            return events
        events.append(
            {
                "type": "turn.failed",
                "error": {"message": f"grok turn ended with stopReason {stop}"},
            }
        )
        return events

    def _on_error(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        """Fatal ``error`` line (auth failure, unknown model, ...)."""
        events = self._ensure_turn_started() + self._flush_segment()
        message = _error_message(obj)
        events.append({"type": "error", "message": message})
        events.append({"type": "turn.failed", "error": {"message": message}})
        return events

    # -- streaming-messages-json (defensive; not the requested format) ------

    def _on_assistant(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        events = self._ensure_turn_started()
        message = obj.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return events or [_NOOP]
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("thinking", "text"):
                text = block.get("thinking") if btype == "thinking" else block.get("text")
                if not isinstance(text, str) or not text:
                    continue
                item_type = "reasoning" if btype == "thinking" else "agent_message"
                if item_type == "agent_message":
                    self._emitted_agent_message = True
                events.append(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": self._next_item_id(),
                            "type": item_type,
                            "text": text,
                        },
                    }
                )
            elif btype == "tool_use":
                raw_input = block.get("input")
                tool_obj = {
                    "toolCallId": block.get("id"),
                    "toolName": block.get("name"),
                    "rawInput": raw_input if isinstance(raw_input, dict) else {},
                }
                events.extend(self._on_tool_call(tool_obj))
        return events or [_NOOP]

    def _on_user(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        message = obj.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return [_NOOP]
        events: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            raw_output: Any = block.get("content")
            if isinstance(raw_output, str):
                try:
                    raw_output = json.loads(raw_output)
                except json.JSONDecodeError:
                    raw_output = {}
            output, exit_code = self._tool_output(raw_output)
            call_id = block.get("tool_use_id")
            item_id = call_id if isinstance(call_id, str) and call_id else self._next_item_id()
            open_info = self._open_tools.pop(item_id, {})
            is_error = bool(block.get("is_error"))
            item_status = "failed" if is_error else "completed"
            if exit_code is None and item_status == "completed":
                exit_code = 0
            events.extend(self._ensure_turn_started())
            events.extend(self._flush_segment())
            events.append(
                {
                    "type": "item.completed",
                    "item": self._tool_item(
                        item_id,
                        str(open_info.get("name") or ""),
                        str(open_info.get("kind") or ""),
                        open_info.get("raw_input")
                        if isinstance(open_info.get("raw_input"), dict)
                        else {},
                        block,
                        status=item_status,
                        output=output,
                        exit_code=exit_code,
                    ),
                }
            )
        return events or [_NOOP]

    def _on_result(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        events = self._ensure_turn_started()
        events.extend(self._thread_or_stale(self._session_marker(obj)))
        events.extend(self._flush_segment())
        if self._stale:
            events.append(
                {
                    "type": "turn.failed",
                    "error": {"message": f'resume failed: session "{self._expected_id}" not found'},
                }
            )
            return events
        is_error = bool(obj.get("is_error")) or str(obj.get("subtype") or "") not in (
            "",
            "success",
        )
        if not is_error:
            result_text = obj.get("result")
            if isinstance(result_text, str) and result_text and not self._emitted_agent_message:
                self._emitted_agent_message = True
                events.append(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": self._next_item_id(),
                            "type": "agent_message",
                            "text": result_text,
                        },
                    }
                )
            events.append({"type": "turn.completed", "usage": _map_usage(obj.get("usage"))})
            return events
        message = _error_message(obj)
        if message == "grok turn failed":
            result_text = obj.get("result")
            if isinstance(result_text, str) and result_text:
                message = result_text
        events.append({"type": "turn.failed", "error": {"message": message}})
        return events
