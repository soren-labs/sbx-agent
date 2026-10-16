"""Text PostgreSQL can store.

PostgreSQL rejects NUL in ``text`` and in JSONB strings (``\\u0000`` is an
"unsupported Unicode escape sequence"), and lone UTF-16 surrogates cannot be encoded
as UTF-8 at all. Runtime evidence (tool output, file names) may contain either, so
values are made storable deterministically: each such code point becomes U+FFFD.
The same input always yields the same output, so replayed evidence still compares equal.
"""

from __future__ import annotations

import re
from typing import Any

REPLACEMENT = "�"
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


def storable(value: Any) -> tuple[Any, int]:
    """``value`` with unstorable code points replaced, and how many were replaced."""
    if isinstance(value, str):
        if not _UNSTORABLE.search(value):
            return value, 0
        return _UNSTORABLE.subn(REPLACEMENT, value)
    if isinstance(value, dict):
        count = 0
        out: dict[Any, Any] = {}
        for k, v in value.items():
            key, n = storable(k)
            out[key], m = storable(v)
            count += n + m
        return (out, count) if count else (value, 0)
    if isinstance(value, list | tuple):
        items = [storable(v) for v in value]
        count = sum(n for _, n in items)
        if not count:
            return value, 0
        return type(value)(v for v, _ in items), count
    return value, 0
