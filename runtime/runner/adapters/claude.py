"""Claude Code CLI adapter (SOR-97 — EXPERIMENTAL, not registered).

Drives ``claude -p <PROMPT> --output-format stream-json --verbose
[--model <M>] --permission-mode bypassPermissions`` per the SOR-97 spike
(``docs/reviews/SOR-97.md``): one process per turn, ``--resume <uuid>``
for follow-ups, stdin DEVNULL (runner contract). ``CLAUDE_BIN`` overrides
the CLI binary (tests point it at ``tests/unit/runner/replay_claude.py``).

``claude`` is **not** in the frozen provider set
(``runtime/runner/adapter.py`` ``PROVIDERS``), so this module is never
reached through ``get_adapter``; the runner-level wiring is the SOR-96
seven-line diff, deferred until a real Modal gate passes. See the review
note for the credential story: the file channel is
``~/.claude/.credentials.json`` (verified: a blob placed there is honoured
by ``claude auth status``), while the OAuth path on a developer machine
typically lives in the OS keyring and is **not** file-portable.

Verified against the installed CLI (2.1.250) live captures — ``init``,
``assistant`` API-error, ``result`` success/error and stale-resume shapes
are real; success-path content blocks (``thinking``/``text``/
``tool_use``/``tool_result``) follow the Messages-API block model
confirmed by session transcripts:

- ``system`` ``subtype=="init"`` -> ``thread.started{thread_id:
  session_id}`` (first line of every stream) + ``turn.started``. The
  top-level ``session_id`` on any non-terminal-error line can serve as
  the marker (events.md rule 1).
- ``assistant`` -> content blocks: ``thinking`` -> ``item.completed``
  ``reasoning``; ``text`` -> ``item.completed`` ``agent_message``;
  ``tool_use`` -> ``item.started`` keyed by the block ``id``
  (``Write``/``Edit``/``MultiEdit``/``NotebookEdit`` -> ``file_change``;
  ``Bash`` and the rest -> ``command_execution``). Lines flagged
  ``is_api_error_message`` / ``error`` (the real CLI reports auth and
  API failures this way, ``model`` ``"<synthetic>"``) ->
  ``error{message}``; the turn still closes on the ``result`` line.
- ``user`` -> ``tool_result`` blocks -> ``item.completed`` closing the
  matching ``tool_use_id``; a first-sight terminal emits
  started+completed together (agy/opencode precedent). ``is_error``
  marks the item failed.
- ``result`` -> terminal: ``is_error`` or a non-``success`` ``subtype``
  (``error_during_execution``, ...) -> ``error{message}`` +
  ``turn.failed``; otherwise ``turn.completed{usage}`` with
  ``cache_read_input_tokens`` -> ``cached_input_tokens``,
  ``cache_creation_input_tokens`` -> ``cache_write_input_tokens`` and
  ``output_tokens_details.thinking_tokens`` ->
  ``reasoning_output_tokens`` (real 2.1.250 field names).
- ``stream_event`` (``--include-partial-messages`` partials; never
  requested on argv) and unknown-but-parseable kinds -> NOOP (SOR-80
  forward compatibility). ``[]`` is reserved for lines with no JSON
  object, which the runner counts as bad JSON (events.md rule 5).

Exit-code caveat (verified): the real CLI exits **rc=0 even on auth/API
failure** — the terminal signal is the ``result`` line, not the exit
code. Stale ``--resume`` ids print ``No conversation found with session
ID: ...`` on stderr and emit a ``result`` ``subtype=error_during_execution``
line on stdout, still rc=0. ``health_from`` therefore also consults flags
recorded while translating error lines, though the current runner only
calls it on nonzero rc (see SOR-97 review note §known-limitations).
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

# Verified: a blob at ``~/.claude/.credentials.json`` is honoured by
# ``claude auth status`` (SOR-97 fresh-HOME probe). The alternative env
# channels (``CLAUDE_CODE_OAUTH_TOKEN`` from ``claude setup-token``,
# ``ANTHROPIC_API_KEY``) are env-level and excluded from the child env
# so the restored file stays the only credential source.
CLAUDE_CREDENTIALS_REL = ".claude/.credentials.json"

# Alternate auth/model/provider channels that must never reach the
# ``claude`` child (future AGENT_ENV_EXCLUDE union entry; SOR-97 spike).
CLAUDE_ENV_EXCLUDE: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)

_AUTH_NEEDLES = (
    "not logged in",
    "not signed in",
    "please run /login",
    "unauthorized",
    "unauthenticated",
    "authentication_failed",
    "invalid api key",
    "error_account_closed",
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

# Claude Code built-in tools that produce file_change items; ``Bash``
# and the rest produce command_execution.
_FILE_TOOLS = {"write", "edit", "multiedit", "multi_edit", "notebookedit", "notebook_edit"}

# Result subtypes that mean the turn failed despite rc=0 (stale resume
# capture: ``error_during_execution``). ``success`` is the only
# successful terminal subtype.
_RESULT_FAILURE_SUBTYPES = ("error_",)

_NOOP = {"type": NOOP_EVENT_TYPE}


def claude_bin_tokens() -> list[str]:
    raw = os.environ.get("CLAUDE_BIN", "claude")
    tokens = shlex.split(raw)
    if not tokens:
        tokens = ["claude"]
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


def _map_usage(raw: Any) -> dict[str, int]:
    usage = _empty_usage()
    if not isinstance(raw, dict):
        return usage
    usage["input_tokens"] = _as_int(raw.get("input_tokens"))
    usage["cached_input_tokens"] = _as_int(raw.get("cache_read_input_tokens"))
    usage["cache_write_input_tokens"] = _as_int(raw.get("cache_creation_input_tokens"))
    usage["output_tokens"] = _as_int(raw.get("output_tokens"))
    details = raw.get("output_tokens_details")
    if isinstance(details, dict):
        usage["reasoning_output_tokens"] = _as_int(details.get("thinking_tokens"))
    return usage


def _flatten_text(content: Any) -> str:
    """``tool_result``/message content: string or list of text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if isinstance(part, str) and part)
    return ""


def _result_message(obj: dict[str, Any]) -> str:
    """Terminal message: flat ``result`` string, else ``errors[]``."""
    result = obj.get("result")
    if isinstance(result, str) and result:
        return result
    errors = obj.get("errors")
    if isinstance(errors, list):
        for entry in errors:
            if isinstance(entry, str) and entry:
                return entry
    subtype = obj.get("subtype")
    if isinstance(subtype, str) and subtype:
        return f"claude turn ended with subtype {subtype}"
    return "claude turn failed"


class ClaudeAdapter:
    """Claude Code (``claude -p --output-format stream-json``) -> canonical events."""

    provider = "claude"
    credential_files: tuple[str, ...] = (CLAUDE_CREDENTIALS_REL,)

    def __init__(self) -> None:
        self._item_seq = 0
        self._expected_id: str | None = None
        self._stale = False
        self._thread_seen = False
        self._turn_started = False
        self._turn_terminal = False
        self._auth_seen = False
        self._rate_seen = False
        # tool_use_id -> {"name", "input"} for open calls.
        self._open_tools: dict[str, dict[str, Any]] = {}

    def prepare_home(self, home: Path, model: str) -> None:
        """Create ``~/.claude`` (700); keep ``.credentials.json`` at 600.

        The credential blob restores ``.claude/.credentials.json`` before
        this runs; ``prepare_home`` never writes its contents. The CLI
        self-populates the rest (``~/.claude.json``, ``projects/``,
        ``settings.json``) — ``$HOME`` only needs to be writable.
        Approval bypass is argv-level (``--permission-mode
        bypassPermissions``) and ``model`` travels on argv, so no config
        file is written.
        """
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        claude_dir.chmod(0o700)
        cred = claude_dir / ".credentials.json"
        if cred.is_file():
            cred.chmod(0o600)

    def first_turn_argv(self, prompt: str, model: str) -> list[str]:
        argv = [*claude_bin_tokens(), "-p", prompt, "--output-format", "stream-json"]
        if model:
            argv += ["--model", model]
        argv += ["--verbose", "--permission-mode", "bypassPermissions"]
        return argv

    def resume_argv(self, prompt: str, native_session_id: str) -> list[str]:
        self._expected_id = native_session_id
        argv = [
            *claude_bin_tokens(),
            "-p",
            prompt,
            "--resume",
            native_session_id,
            "--output-format",
            "stream-json",
        ]
        model = _session_model()
        if model:
            argv += ["--model", model]
        argv += ["--verbose", "--permission-mode", "bypassPermissions"]
        return argv

    def translate(self, raw_line: str) -> list[dict[str, Any]]:
        obj, bad = parse_event_line(raw_line)
        if bad or obj is None:
            return []
        kind = obj.get("type")
        if kind == "result":
            return self._on_result(obj)
        events = self._thread_or_stale(self._session_marker(obj))
        if self._stale:
            return events or [_NOOP]
        if kind == "assistant":
            events += self._on_assistant(obj)
        elif kind == "user":
            events += self._on_user(obj)
        elif kind == "system" and obj.get("subtype") == "init":
            events += self._ensure_turn_started()
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
        # The real CLI reports auth/rate failures as stream events with
        # rc=0; flags recorded by ``translate`` outrank the exit code.
        if self._auth_seen:
            return "auth_invalid"
        if self._rate_seen:
            return "rate_limited"
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

    def _flag_health(self, text: str) -> None:
        tail = text.lower()
        if any(needle in tail for needle in _AUTH_NEEDLES):
            self._auth_seen = True
        if any(needle in tail for needle in _RATE_NEEDLES):
            self._rate_seen = True

    def _session_marker(self, obj: dict[str, Any]) -> str | None:
        sid = obj.get("session_id")
        if isinstance(sid, str) and sid:
            return sid
        return None

    def _thread_or_stale(self, sid: str | None) -> list[dict[str, Any]]:
        """Emit thread.started for ``sid`` unless it contradicts a requested
        ``--resume`` id (defensive; a real stale id ends in an
        ``error_during_execution`` result instead)."""
        if not sid or self._thread_seen or self._stale:
            return []
        if self._expected_id is not None and sid != self._expected_id:
            self._stale = True
            return [
                {
                    "type": "error",
                    "message": (
                        f'session "{self._expected_id}" not found; claude reported session "{sid}"'
                    ),
                }
            ]
        self._thread_seen = True
        return [{"type": "thread.started", "thread_id": sid}]

    def _fail_stale(self) -> list[dict[str, Any]]:
        if self._turn_terminal:
            return []
        self._turn_terminal = True
        return [
            {
                "type": "turn.failed",
                "error": {"message": f'resume failed: session "{self._expected_id}" not found'},
            }
        ]

    def _on_assistant(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        message = obj.get("message")
        if not isinstance(message, dict):
            return [_NOOP]
        content = message.get("content")
        if not isinstance(content, list):
            content = []
        # API/auth failure line: synthetic assistant message whose text is
        # the error (real capture: ``error``, ``is_api_error_message``).
        if obj.get("is_api_error_message") or obj.get("error"):
            text = _flatten_text(content)
            if not text:
                error = obj.get("error")
                text = error if isinstance(error, str) and error else "claude api error"
            self._flag_health(text)
            return self._ensure_turn_started() + [{"type": "error", "message": text}]
        events = self._ensure_turn_started()
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "thinking":
                text = block.get("thinking")
                if isinstance(text, str) and text:
                    events.append(self._text_item("reasoning", text))
            elif btype == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    events.append(self._text_item("agent_message", text))
            elif btype == "tool_use":
                events += self._on_tool_use(block)
            # Other block kinds (redacted_thinking, server_tool_use, ...)
            # are forward-compatible: no canonical event.
        return events or [_NOOP]

    def _text_item(self, item_type: str, text: str) -> dict[str, Any]:
        return {
            "type": "item.completed",
            "item": {"id": self._next_item_id(), "type": item_type, "text": text},
        }

    def _tool_item(
        self,
        item_id: str,
        name: str,
        raw_input: dict[str, Any],
        *,
        status: str,
        output: str = "",
        exit_code: int | None = None,
    ) -> dict[str, Any]:
        if name.lower() in _FILE_TOOLS:
            path = (
                raw_input.get("file_path")
                or raw_input.get("notebook_path")
                or raw_input.get("path")
                or ""
            )
            kind = "add" if name.lower() == "write" else "update"
            return {
                "id": item_id,
                "type": "file_change",
                "changes": [{"path": str(path), "kind": kind}],
                "status": status,
            }
        command = raw_input.get("command")
        if not command:
            command = name
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

    def _on_tool_use(self, block: dict[str, Any]) -> list[dict[str, Any]]:
        call_id = block.get("id")
        item_id = call_id if isinstance(call_id, str) and call_id else self._next_item_id()
        name = str(block.get("name") or "")
        raw_input = block.get("input")
        if not isinstance(raw_input, dict):
            raw_input = {}
        self._open_tools[item_id] = {"name": name, "input": raw_input}
        return [
            {
                "type": "item.started",
                "item": self._tool_item(item_id, name, raw_input, status="in_progress"),
            }
        ]

    def _on_user(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        message = obj.get("message")
        if not isinstance(message, dict):
            return [_NOOP]
        content = message.get("content")
        if not isinstance(content, list):
            return [_NOOP]
        events = self._ensure_turn_started()
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            events += self._on_tool_result(block)
        return events or [_NOOP]

    def _on_tool_result(self, block: dict[str, Any]) -> list[dict[str, Any]]:
        call_id = block.get("tool_use_id")
        item_id = call_id if isinstance(call_id, str) and call_id else self._next_item_id()
        open_info = self._open_tools.pop(item_id, {})
        name = str(open_info.get("name") or "")
        raw_input = open_info.get("input")
        if not isinstance(raw_input, dict):
            raw_input = {}
        failed = bool(block.get("is_error"))
        status = "failed" if failed else "completed"
        output = _flatten_text(block.get("content"))
        events: list[dict[str, Any]] = []
        if not open_info:
            # First sight is already terminal: emit started+completed
            # together per the item contract (agy/opencode precedent).
            events.append(
                {
                    "type": "item.started",
                    "item": self._tool_item(item_id, name, raw_input, status="in_progress"),
                }
            )
        exit_code: int | None = 1 if failed else 0
        events.append(
            {
                "type": "item.completed",
                "item": self._tool_item(
                    item_id,
                    name,
                    raw_input,
                    status=status,
                    output=output,
                    exit_code=exit_code,
                ),
            }
        )
        return events

    def _on_result(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        subtype = str(obj.get("subtype") or "")
        is_error = bool(obj.get("is_error")) or subtype.startswith(_RESULT_FAILURE_SUBTYPES)
        if is_error:
            # A failed result never opened a session (stale --resume
            # echoes the *requested* id): suppress the marker so the
            # runner's stale-resume check also sees no adoption.
            message = _result_message(obj)
            self._flag_health(message)
            if self._stale:
                return events + self._fail_stale()
            events.append({"type": "error", "message": message})
            if not self._turn_terminal:
                self._turn_terminal = True
                events.append({"type": "turn.failed", "error": {"message": message}})
            return events
        events += self._thread_or_stale(self._session_marker(obj))
        if self._stale:
            return events + self._fail_stale()
        if not self._turn_terminal:
            self._turn_terminal = True
            events.append({"type": "turn.completed", "usage": _map_usage(obj.get("usage"))})
        return events or [_NOOP]
