#!/usr/bin/env python3
"""Replay a staged ``opencode run --format json`` capture as provider stdout.

Test-only (SOR-96). Speaks the production argv contract (``opencode run
<PROMPT> --format json [-m <provider/model>] [--session <id>] --dir
$SBX_WORK --auto``) per the OpenCode CLI reference: ``run`` takes the prompt
as a positional, ``-s/--session`` continues a session. Replays
``OPENCODE_REPLAY_FIXTURE`` — a staged capture under
``tests/unit/runner/fixtures/opencode/`` (or a WP0 fake fixture).

Env knobs:
- ``OPENCODE_REPLAY_SESSION_ID``: rewrite every ``sessionID`` (top level and
  ``part.sessionID``).
- ``OPENCODE_REPLAY_STALE=1``: with ``--session <id>``, mimic a stale-id
  failure — stderr "Error: Session ... not found", rc=1, **empty stdout**
  (no stream error event).
- ``OPENCODE_REPLAY_STDERR``: one extra stderr line (e.g. an auth banner).
- ``OPENCODE_REPLAY_RC``: process exit code (default 0).
- ``OPENCODE_REPLAY_PAUSE_S``: sleep N seconds after the first line.
- ``OPENCODE_REPLAY_HANG=1``: emit the first line then block until SIGTERM.
- ``OPENCODE_REPLAY_SPY_OUT``: write {argv, stdin_target, env flags} JSON
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
    "-m",
    "--model",
    "-s",
    "--session",
    "-C",
    "--cd",
    "--dir",
    "--format",
    "--agent",
    "-f",
    "--file",
    "--title",
    "--variant",
    "--attach",
    "--port",
    "-e",
    "--env",
}
_BOOL_FLAGS = {
    "--auto",
    "--print-logs",
    "--continue",
    "-c",
    "--last",
    "--share",
    "--fork",
    "-p",
    "--pure",
    "-v",
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
    if isinstance(obj.get("sessionID"), str):
        obj["sessionID"] = new_id
    part = obj.get("part")
    if isinstance(part, dict) and isinstance(part.get("sessionID"), str):
        part["sessionID"] = new_id
    return json.dumps(obj, ensure_ascii=False)


def _spy() -> None:
    dump = os.environ.get("OPENCODE_REPLAY_SPY_OUT")
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
                "has_OPENCODE_CONFIG": "OPENCODE_CONFIG" in os.environ,
                "has_OPENCODE_CONFIG_CONTENT": "OPENCODE_CONFIG_CONTENT" in os.environ,
                "has_ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY" in os.environ,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _term(_signum: int, _frame: object) -> None:
    sys.exit(0)


def main() -> None:
    argv = sys.argv[1:]
    # ``run`` is a positional subcommand, not a flag — strip it like the real
    # CLI does before flag scanning.
    if argv and argv[0] == "run":
        argv = argv[1:]
    values = _scan(argv)
    _spy()

    fixture = os.environ.get("OPENCODE_REPLAY_FIXTURE")
    if not fixture:
        print("OPENCODE_REPLAY_FIXTURE is required", file=sys.stderr)
        raise SystemExit(1)

    requested = values.get("--session") or values.get("-s")
    if os.environ.get("OPENCODE_REPLAY_STALE") == "1" and requested:
        # Stale --session id: the CLI fails on stderr, rc=1, empty stdout.
        print(f'Error: Session "{requested}" not found', file=sys.stderr)
        raise SystemExit(1)
    extra = os.environ.get("OPENCODE_REPLAY_STDERR")
    if extra:
        print(extra, file=sys.stderr)

    new_id = os.environ.get("OPENCODE_REPLAY_SESSION_ID") or None
    hang = os.environ.get("OPENCODE_REPLAY_HANG") == "1"
    pause = float(os.environ.get("OPENCODE_REPLAY_PAUSE_S") or "0")

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

    raise SystemExit(int(os.environ.get("OPENCODE_REPLAY_RC", "0")))


if __name__ == "__main__":
    main()
