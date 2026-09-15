#!/usr/bin/env python3
"""Replay a staged ``devin -p`` NDJSON capture as provider stdout (SOR-80).

Test-only counterpart to ``replay_grok.py``/``replay_agy.py`` for the
``SBX_DEVIN_TRANSPORT=cli`` path: ignores argv (the fake surface is just
``devin -p <PROMPT>`` / ``devin --resume <id> -p <PROMPT>``), replays
``DEVIN_REPLAY_FIXTURE`` line-for-line, and exits ``DEVIN_REPLAY_RC``
(default 0).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    fixture = os.environ.get("DEVIN_REPLAY_FIXTURE")
    if not fixture:
        print("DEVIN_REPLAY_FIXTURE is required", file=sys.stderr)
        raise SystemExit(1)
    for raw in Path(fixture).read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        sys.stdout.write(raw + "\n")
        sys.stdout.flush()
    raise SystemExit(int(os.environ.get("DEVIN_REPLAY_RC", "0")))


if __name__ == "__main__":
    main()
