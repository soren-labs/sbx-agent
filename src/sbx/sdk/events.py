"""Committed Session event streaming (SSE) with cursor resume."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any


def parse_sse(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    data: list[str] = []
    for line in lines:
        if line == "":
            if data:
                yield json.loads("\n".join(data))
                data = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field == "data":
            data.append(value.lstrip(" "))
    if data:
        yield json.loads("\n".join(data))
