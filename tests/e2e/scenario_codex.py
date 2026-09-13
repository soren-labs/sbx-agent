#!/usr/bin/env python3
"""CODEX_BIN wrapper: pick fake_codex scenario from the user prompt.

Used only by Playwright against the real control plane (WP2-G). Does not
change fake_codex semantics.

- prompt contains ``hang`` → hang (stop / idle)
- argv includes ``resume`` → resume fixture (history + file update)
- otherwise → success
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

FAKE = Path(__file__).resolve().parents[1] / "fakes" / "fake_codex.py"


def choose_scenario(argv: list[str]) -> str:
    prompt = ""
    for tok in reversed(argv):
        if tok.startswith("-") or tok in {"exec", "resume"}:
            continue
        prompt = tok
        break
    if "hang" in prompt.lower():
        return "hang"
    if "resume" in argv:
        return "resume"
    return "success"


def main() -> None:
    if not FAKE.is_file():
        print(f"fake_codex not found: {FAKE}", file=sys.stderr)
        sys.exit(1)
    os.environ["FAKE_CODEX_SCENARIO"] = choose_scenario(sys.argv[1:])
    os.execv(sys.executable, [sys.executable, str(FAKE), *sys.argv[1:]])


if __name__ == "__main__":
    main()
