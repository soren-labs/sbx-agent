#!/usr/bin/env python3
"""Replay a staged real ``agy`` capture as provider stdout. Test-only (SOR-62).

Speaks the production argv contract (``agy -p <PROMPT> --output-format
stream-json [--conversation <id>] [--model <m>] --dangerously-skip-permissions
--disable-slash-commands``) and replays ``AGY_REPLAY_FIXTURE`` — a staged
SOR-60 recording under ``spike/p2/fixtures/antigravity/``.

Env knobs:
- ``AGY_REPLAY_SESSION_ID``: rewrite every ``conversation_id`` (top level and
  nested in ``init`` / ``step_update`` / ``result``).
- ``AGY_REPLAY_STALE=1``: with ``--conversation <id>``, print the real
  ``warning: conversation "<id>" not found`` to stderr (the CLI then exits 0
  having opened a new conversation; pair with AGY_REPLAY_SESSION_ID).
- ``AGY_REPLAY_STDERR``: one extra stderr line (e.g. the auth-invalid banner).
- ``AGY_REPLAY_RC``: process exit code (default 0).
- ``AGY_REPLAY_PAUSE_S``: sleep N seconds after the first line.
- ``AGY_REPLAY_HANG=1``: emit the first line then block until SIGTERM.
- ``AGY_REPLAY_SPY_OUT``: write {argv, stdin_target, env flags} JSON there.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

_VALUE_FLAGS = {
    "--output-format",
    "--input-format",
    "--model",
    "-m",
    "--conversation",
    "--print-timeout",
    "--effort",
    "--mode",
}
_BOOL_FLAGS = {
    "-p",
    "--print",
    "--prompt",
    "--dangerously-skip-permissions",
    "--disable-slash-commands",
    "-c",
    "--continue",
    "--sandbox",
    "--yolo",
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
    if isinstance(obj.get("conversation_id"), str):
        obj["conversation_id"] = new_id
    for key in ("init", "step_update", "result"):
        inner = obj.get(key)
        if isinstance(inner, dict) and isinstance(inner.get("conversation_id"), str):
            inner["conversation_id"] = new_id
    return json.dumps(obj, ensure_ascii=False)


def _spy() -> None:
    dump = os.environ.get("AGY_REPLAY_SPY_OUT")
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
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _term(_signum: int, _frame: object) -> None:
    sys.exit(0)


def main() -> None:
    values = _scan(sys.argv[1:])
    _spy()

    fixture = os.environ.get("AGY_REPLAY_FIXTURE")
    if not fixture:
        print("AGY_REPLAY_FIXTURE is required", file=sys.stderr)
        raise SystemExit(1)

    requested = values.get("--conversation")
    if os.environ.get("AGY_REPLAY_STALE") == "1" and requested:
        print(f'warning: conversation "{requested}" not found', file=sys.stderr)
    extra = os.environ.get("AGY_REPLAY_STDERR")
    if extra:
        print(extra, file=sys.stderr)

    new_id = os.environ.get("AGY_REPLAY_SESSION_ID") or None
    hang = os.environ.get("AGY_REPLAY_HANG") == "1"
    pause = float(os.environ.get("AGY_REPLAY_PAUSE_S") or "0")

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

    raise SystemExit(int(os.environ.get("AGY_REPLAY_RC", "0")))


if __name__ == "__main__":
    main()
