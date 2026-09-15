#!/usr/bin/env python3
"""Replay a staged real ``grok`` capture as provider stdout. Test-only (SOR-62).

Speaks the production argv contract (``grok -p <PROMPT> --output-format
streaming-json [--resume <id>] [--model <m>] --permission-mode
bypassPermissions``) per spike/p2/GROK_SPIKE.md: ``-p`` takes the prompt
**value** and resume is ``--resume <id>`` (``-s/--session-id`` only names
a new session). Replays ``GROK_REPLAY_FIXTURE`` — a staged SOR-60 recording
under ``tests/unit/runner/fixtures/grok/`` (or a WP0 fake fixture).

Env knobs:
- ``GROK_REPLAY_SESSION_ID``: rewrite every ``sessionId``/``session_id``.
- ``GROK_REPLAY_STALE=1``: with ``--resume <id>``, mimic the real stale-id
  signature — stderr "Session ... not found locally, restoring
  conversation from remote..." + "Error: Failed to restore session from
  remote: ... 404 Not Found", rc=1, **empty stdout** (no stream error).
- ``GROK_REPLAY_STDERR``: one extra stderr line (e.g. the not-signed-in
  banner).
- ``GROK_REPLAY_RC``: process exit code (default 0).
- ``GROK_REPLAY_PAUSE_S``: sleep N seconds after the first line.
- ``GROK_REPLAY_HANG=1``: emit the first line then block until SIGTERM.
- ``GROK_REPLAY_SPY_OUT``: write {argv, stdin_target, env flags} JSON there.
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
    "--single",
    "--output-format",
    "--input-format",
    "--model",
    "-m",
    "--resume",
    "-r",
    "--session-id",
    "-s",
    "--permission-mode",
    "-C",
    "--cd",
    "--prompt-file",
    "--prompt-json",
    "--max-turns",
    "--effort",
    "--reasoning-effort",
    "--sandbox",
}
_BOOL_FLAGS = {
    "--print",
    "--continue",
    "-c",
    "--last",
    "--fork-session",
    "--include-partial-messages",
    "--no-plan",
    "--no-subagents",
    "--disable-web-search",
    "--worktree",
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
    for key in ("sessionId", "session_id"):
        if isinstance(obj.get(key), str):
            obj[key] = new_id
    return json.dumps(obj, ensure_ascii=False)


def _spy() -> None:
    dump = os.environ.get("GROK_REPLAY_SPY_OUT")
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
                "has_XAI_API_KEY": "XAI_API_KEY" in os.environ,
                "has_GROK_AUTH_PROVIDER_ACCESS_TOKEN": (
                    "GROK_AUTH_PROVIDER_ACCESS_TOKEN" in os.environ
                ),
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

    fixture = os.environ.get("GROK_REPLAY_FIXTURE")
    if not fixture:
        print("GROK_REPLAY_FIXTURE is required", file=sys.stderr)
        raise SystemExit(1)

    requested = values.get("--resume") or values.get("-r")
    if os.environ.get("GROK_REPLAY_STALE") == "1" and requested:
        # Real stale-id signature: local miss -> remote restore -> 404,
        # rc=1, stdout completely empty.
        print(
            f'Session "{requested}" not found locally, restoring conversation from remote...',
            file=sys.stderr,
        )
        print(
            "Error: Failed to restore session from remote: HttpError(404 Not Found)",
            file=sys.stderr,
        )
        raise SystemExit(1)
    extra = os.environ.get("GROK_REPLAY_STDERR")
    if extra:
        print(extra, file=sys.stderr)

    new_id = os.environ.get("GROK_REPLAY_SESSION_ID") or None
    hang = os.environ.get("GROK_REPLAY_HANG") == "1"
    pause = float(os.environ.get("GROK_REPLAY_PAUSE_S") or "0")

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

    raise SystemExit(int(os.environ.get("GROK_REPLAY_RC", "0")))


if __name__ == "__main__":
    main()
