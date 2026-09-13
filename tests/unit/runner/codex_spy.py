#!/usr/bin/env python3
"""Record argv / stdin / env, then delegate to fake_codex. Test-only."""

from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path


def main() -> None:
    dump = os.environ.get("CODEX_SPY_OUT")
    if dump:
        stdin_target = None
        try:
            stdin_target = os.readlink("/proc/self/fd/0")
        except OSError:
            stdin_target = None
        Path(dump).write_text(
            json.dumps(
                {
                    "argv": sys.argv[1:],
                    "has_CODEX_AUTH_JSON": "CODEX_AUTH_JSON" in os.environ,
                    "stdin_isatty": sys.stdin.isatty(),
                    "stdin_target": stdin_target,
                }
            )
            + "\n",
            encoding="utf-8",
        )
    target = os.environ.get("CODEX_SPY_TARGET")
    if not target:
        raise SystemExit(0)
    sys.argv[0] = target
    runpy.run_path(target, run_name="__main__")


if __name__ == "__main__":
    main()
