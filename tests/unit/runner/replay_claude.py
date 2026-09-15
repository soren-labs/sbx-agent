#!/usr/bin/env python3
"""Replay a staged ``claude -p --output-format stream-json`` capture as
provider stdout.

Test-only (SOR-97). Speaks the production argv contract (``claude -p
<PROMPT> --output-format stream-json [--model <M>] --verbose
--permission-mode bypassPermissions``; resume adds ``--resume <uuid>``)
per the verified 2.1.250 CLI surface. Replays
``CLAUDE_REPLAY_FIXTURE`` — a staged capture under
``tests/unit/runner/fixtures/claude/``.

Env knobs:
- ``CLAUDE_REPLAY_SESSION_ID``: rewrite every top-level ``session_id``.
- ``CLAUDE_REPLAY_STALE=1``: with ``--resume <id>``, mimic the real
  stale-id failure — stderr ``No conversation found with session ID:
  <id>`` plus a single ``result`` ``subtype=error_during_execution``
  line on stdout, rc=0 (real capture, SOR-97).
- ``CLAUDE_REPLAY_STDERR``: one extra stderr line.
- ``CLAUDE_REPLAY_RC``: process exit code (default 0).
- ``CLAUDE_REPLAY_PAUSE_S``: sleep N seconds after the first line.
- ``CLAUDE_REPLAY_HANG=1``: emit the first line then block until SIGTERM.
- ``CLAUDE_REPLAY_SPY_OUT``: write {argv, stdin_target, env flags} JSON
  there.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

_VALUE_FLAGS = {
    "-p",
    "--print",
    "--model",
    "-m",
    "--resume",
    "-r",
    "--session-id",
    "--output-format",
    "--input-format",
    "--permission-mode",
    "--settings",
    "--add-dir",
    "--agent",
    "--effort",
    "--fallback-model",
    "--max-budget-usd",
    "--system-prompt",
    "--append-system-prompt",
    "--setting-sources",
}
_BOOL_FLAGS = {
    "--verbose",
    "-v",
    "--fork-session",
    "--continue",
    "-c",
    "--bare",
    "--include-partial-messages",
    "--include-hook-events",
    "--forward-subagent-text",
    "--dangerously-skip-permissions",
    "--no-session-persistence",
    "--strict-mcp-config",
    "--disable-slash-commands",
}


def _scan(argv: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _VALUE_FLAGS:
            values[tok] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
        else:
            i += 1
    return values


def _rewrite_ids(line: str, new_id: str) -> str:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return line
    if not isinstance(obj, dict):
        return line
    if isinstance(obj.get("session_id"), str):
        obj["session_id"] = new_id
    return json.dumps(obj, ensure_ascii=False)


def _spy() -> None:
    dump = os.environ.get("CLAUDE_REPLAY_SPY_OUT")
    if not dump:
        return
    try:
        stdin_target = os.readlink("/proc/self/fd/0")
    except OSError:
        stdin_target = None
    Path(dump).write_text(
        json.dumps(
            {
                "argv": sys.argv[1:],
                "stdin_target": stdin_target,
                "stdin_isatty": sys.stdin.isatty(),
                "has_SBX_ACCOUNT_CREDENTIAL": "SBX_ACCOUNT_CREDENTIAL" in os.environ,
                "has_CODEX_AUTH_JSON": "CODEX_AUTH_JSON" in os.environ,
                "has_ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY" in os.environ,
                "has_CLAUDE_CODE_OAUTH_TOKEN": "CLAUDE_CODE_OAUTH_TOKEN" in os.environ,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _term(_signum: int, _frame: object) -> None:
    sys.exit(0)


def _emit_stale(requested: str) -> None:
    """Real stale-resume shape: stderr notice + one error result, rc=0."""
    print(f"No conversation found with session ID: {requested}", file=sys.stderr)
    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "duration_ms": 0,
                "duration_api_ms": 0,
                "is_error": True,
                "num_turns": 0,
                "stop_reason": None,
                "session_id": requested,
                "total_cost_usd": 0,
                "usage": {
                    "input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 0,
                },
                "permission_denials": [],
                "errors": [f"No conversation found with session ID: {requested}"],
            }
        )
    )


def main() -> None:
    argv = sys.argv[1:]
    values = _scan(argv)
    _spy()

    requested = values.get("--resume") or values.get("-r")
    if os.environ.get("CLAUDE_REPLAY_STALE") == "1" and requested:
        _emit_stale(requested)
        raise SystemExit(int(os.environ.get("CLAUDE_REPLAY_RC", "0")))

    fixture = os.environ.get("CLAUDE_REPLAY_FIXTURE")
    if not fixture:
        print("CLAUDE_REPLAY_FIXTURE is required", file=sys.stderr)
        raise SystemExit(1)
    extra = os.environ.get("CLAUDE_REPLAY_STDERR")
    if extra:
        print(extra, file=sys.stderr)

    new_id = os.environ.get("CLAUDE_REPLAY_SESSION_ID") or None
    hang = os.environ.get("CLAUDE_REPLAY_HANG") == "1"
    pause = float(os.environ.get("CLAUDE_REPLAY_PAUSE_S") or "0")

    first = True
    for raw in Path(fixture).read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        line = _rewrite_ids(raw, new_id) if new_id else raw
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        if first:
            first = False
            if hang:
                signal.signal(signal.SIGTERM, _term)
                signal.signal(signal.SIGINT, _term)
                time.sleep(3600)
                return
            if pause > 0:
                signal.signal(signal.SIGTERM, _term)
                time.sleep(pause)
                signal.signal(signal.SIGTERM, signal.SIG_DFL)

    raise SystemExit(int(os.environ.get("CLAUDE_REPLAY_RC", "0")))


if __name__ == "__main__":
    main()
