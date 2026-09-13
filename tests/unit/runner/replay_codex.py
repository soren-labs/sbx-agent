#!/usr/bin/env python3
"""Replay ``FAKE_CODEX_FIXTURE`` as Codex stdout. Test-only helper for WP1-B."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    path = os.environ.get("FAKE_CODEX_FIXTURE")
    if not path:
        print("FAKE_CODEX_FIXTURE is required", file=sys.stderr)
        raise SystemExit(1)
    text = Path(path).read_text(encoding="utf-8")
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
