"""Durable per-run activity transcripts.

The live SSE stream tails ``events.jsonl`` inside the sandbox, which is gone
once the agent closes or is reclaimed. At turn end the control plane
compacts the run's slice of the event log — final item states only, long
command output trimmed — into a bounded transcript persisted here, so
``GET /v1/agents/{id}/runs/{run}/stream`` can still replay what the agent
did after teardown.

A transcript is a list of ``{"id": <events.jsonl line number>, "event": {...}}``
entries; ids keep the live stream's ``Last-Event-ID`` numbering.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from control.config import RUN_ACTIVITY_DICT_NAME
from control.latency import observe

MAX_TRANSCRIPT_BYTES = 256 * 1024
OUTPUT_KEEP_CHARS = 8 * 1024
OUTPUT_KEEP_CHARS_TIGHT = 1024

_TRANSIENT_ITEM_EVENTS = frozenset({"item.started", "item.updated"})


def _trim(text: str, keep: int) -> str:
    if len(text) <= 2 * keep:
        return text
    dropped = len(text) - 2 * keep
    return f"{text[:keep]}\n… [{dropped} characters trimmed] …\n{text[-keep:]}"


def _trim_outputs(entries: list[dict[str, Any]], keep: int) -> None:
    for entry in entries:
        item = entry["event"].get("item")
        if isinstance(item, dict) and isinstance(item.get("aggregated_output"), str):
            item["aggregated_output"] = _trim(item["aggregated_output"], keep)


def _size(entries: list[dict[str, Any]]) -> int:
    return len(json.dumps(entries, ensure_ascii=False).encode("utf-8"))


def compact_run_events(
    text: str | None, n: int, *, max_bytes: int = MAX_TRANSCRIPT_BYTES
) -> list[dict[str, Any]]:
    """Run ``n``'s slice of an ``events.jsonl`` body as a bounded transcript.

    Turn membership follows ``sbx.turn_started`` boundaries (lines before the
    first boundary belong to run 1). ``item.started``/``item.updated`` are
    dropped when the same item later completes; unparseable lines are skipped.
    """
    if not text:
        return []
    current = 0
    entries: list[dict[str, Any]] = []
    completed: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "sbx.turn_started":
            try:
                current = int(obj.get("n") or 0)
            except (TypeError, ValueError):
                pass
        if not (current == n or (n == 1 and current == 0)):
            continue
        item = obj.get("item")
        if obj.get("type") == "item.completed" and isinstance(item, dict) and item.get("id"):
            completed.add(str(item["id"]))
        entries.append({"id": lineno, "event": obj})
    entries = [
        e
        for e in entries
        if not (
            e["event"].get("type") in _TRANSIENT_ITEM_EVENTS
            and isinstance(e["event"].get("item"), dict)
            and str(e["event"]["item"].get("id")) in completed
        )
    ]
    _trim_outputs(entries, OUTPUT_KEEP_CHARS)
    if _size(entries) <= max_bytes:
        return entries
    _trim_outputs(entries, OUTPUT_KEEP_CHARS_TIGHT)
    dropped = 0
    while entries and _size(entries) > max_bytes:
        entries.pop(0)
        dropped += 1
    if dropped:
        marker = {
            "type": "sbx.error",
            "message": f"activity transcript truncated: {dropped} earlier events omitted",
        }
        entries.insert(0, {"id": max(0, entries[0]["id"] - 1) if entries else 0, "event": marker})
    return entries


def _valid(raw: Any) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list):
        return None
    out = [
        e
        for e in raw
        if isinstance(e, dict) and isinstance(e.get("id"), int) and isinstance(e.get("event"), dict)
    ]
    return out


@runtime_checkable
class RunActivityStore(Protocol):
    def get(self, agent_id: str, n: int) -> list[dict[str, Any]] | None:
        """The stored transcript, or None when absent/undecodable."""

    def put(self, agent_id: str, n: int, entries: list[dict[str, Any]]) -> None:
        """Insert or replace a transcript."""


class InMemoryRunActivityStore:
    def __init__(self) -> None:
        self._items: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def get(self, agent_id: str, n: int) -> list[dict[str, Any]] | None:
        with self._lock:
            raw = self._items.get(f"{agent_id}/{n}")
        return _valid(json.loads(json.dumps(raw))) if raw is not None else None

    def put(self, agent_id: str, n: int, entries: list[dict[str, Any]]) -> None:
        with self._lock:
            self._items[f"{agent_id}/{n}"] = json.loads(json.dumps(entries))


class FileRunActivityStore:
    """One JSON file per run under ``root/<agent_id>/run-<n>.json`` (atomic writes)."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    def _path(self, agent_id: str, n: int) -> Path:
        return self._root / agent_id / f"run-{n}.json"

    def get(self, agent_id: str, n: int) -> list[dict[str, Any]] | None:
        try:
            raw = json.loads(self._path(agent_id, n).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, RecursionError):
            return None
        return _valid(raw)

    def put(self, agent_id: str, n: int, entries: list[dict[str, Any]]) -> None:
        path = self._path(agent_id, n)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)


class ModalDictRunActivityStore:
    """Production store backed by ``modal.Dict``. Lazy-imports modal."""

    def __init__(self, name: str = RUN_ACTIVITY_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    def get(self, agent_id: str, n: int) -> list[dict[str, Any]] | None:
        key = f"{agent_id}/{n}"
        with observe("modal_dict.get", store=self._name, key=key):
            raw = self._d().get(key)
        return _valid(raw) if raw is not None else None

    def put(self, agent_id: str, n: int, entries: list[dict[str, Any]]) -> None:
        key = f"{agent_id}/{n}"
        with observe("modal_dict.put", store=self._name, key=key):
            self._d().put(key, entries)
