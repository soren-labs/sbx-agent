"""JSONL transport framing for official CLIs emitting NDJSON on stdout."""

from __future__ import annotations

import json
from typing import Any


def parse_line(raw: str) -> tuple[dict[str, Any] | None, bool]:
    """Parse one stdout line. Returns (obj, bad): bad=True means the line
    contained no JSON object — adapters count those as malformed frames,
    distinct from parseable-but-unknown kinds."""
    text = raw.strip()
    if not text:
        return None, False
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None, True
    if not isinstance(obj, dict):
        return None, True
    return obj, False
