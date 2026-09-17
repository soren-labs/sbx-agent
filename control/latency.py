"""Latency instrumentation: one structured JSON line per timed op (SOR-118).

Emits ``{"event": "sbx.latency", "op": ..., "ms": ..., "outcome": ...}`` on
the ``sbx.latency`` logger. Fields must be ids / shas / counts / byte sizes
only — never prompts, file contents, or credential material (AGENTS.md §4);
``_safe`` bounds every field to a scalar so a stray object can never dump
state into a log line.

A dedicated stderr handler keeps the lines visible under uvicorn / Modal
even when the root logger is unconfigured; ``propagate`` stays on so tests
and downstream log drains still capture the records. Instrumentation is
best-effort: a logging failure can never break the request path.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_log = logging.getLogger("sbx.latency")
if not _log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _log.addHandler(_handler)
_log.setLevel(logging.INFO)

_FIELD_MAX = 200


def _safe(value: Any) -> Any:
    """Coerce a log field to a bounded scalar — never an object dump."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text if len(text) <= _FIELD_MAX else f"{text[:_FIELD_MAX]}…"


@contextmanager
def observe(op: str, **fields: Any) -> Iterator[None]:
    """Time the block; emit one ``sbx.latency`` JSON line on exit."""
    start = time.monotonic()
    outcome = "ok"
    try:
        yield
    except Exception:
        outcome = "error"
        raise
    finally:
        try:
            payload = {
                "event": "sbx.latency",
                "op": op,
                "ms": round((time.monotonic() - start) * 1000, 1),
                "outcome": outcome,
                **{name: _safe(value) for name, value in fields.items()},
            }
            _log.info(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass


__all__ = ["observe"]
