"""Session persistence: Protocol + InMemoryStore (tests) + ModalDictStore (prod)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from control.backend import SandboxHandle
from control.config import SESSIONS_DICT_NAME


@dataclass
class SessionRecord:
    id: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime
    model: str
    turns: int
    # None = no usage ever reported; /v1 surfaces that as "unavailable"
    # rather than fabricated zeros (SOR-84). /api keeps the zero-filled
    # shape its contract requires.
    usage: dict[str, int] | None
    messages: list[dict[str, Any]]
    owner: str
    sandbox_id: str | None = None
    sandbox_root: str | None = None
    sandbox_tags: dict[str, str] = field(default_factory=dict)
    current_turn_id: str | None = None
    current_turn_n: int | None = None
    last_activity_at: datetime | None = None
    ended_at: datetime | None = None
    # SOR-82: durable Idempotency-Key pin so create dedup survives restarts.
    idempotency_key: str | None = None
    idempotency_fingerprint: str | None = None

    def handle(self) -> SandboxHandle | None:
        if not self.sandbox_id or not self.sandbox_root:
            return None
        return SandboxHandle(
            id=self.sandbox_id,
            root=Path(self.sandbox_root),
            tags=dict(self.sandbox_tags),
        )


@runtime_checkable
class SessionStore(Protocol):
    def get(self, session_id: str) -> SessionRecord | None:
        """Return a copy of the record, or None."""

    def put(self, record: SessionRecord) -> None:
        """Insert or replace a record."""

    def list_all(self) -> list[SessionRecord]:
        """All records (including terminal)."""

    def delete(self, session_id: str) -> None:
        """Remove a record. Used by tests to simulate Dict loss."""


def empty_usage() -> dict[str, int]:
    return {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}


def merge_usage(dst: dict[str, int] | None, src: dict[str, Any] | None) -> dict[str, int] | None:
    if not src:
        return dst
    out = dict(dst or {})
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "cache_write_input_tokens",
        "reasoning_output_tokens",
    ):
        if key in src:
            out[key] = int(out.get(key, 0)) + int(src.get(key) or 0)
    return out


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def record_to_dict(record: SessionRecord) -> dict[str, Any]:
    data = asdict(record)
    for key in ("created_at", "updated_at", "last_activity_at", "ended_at"):
        value = data.get(key)
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data


def record_from_dict(data: dict[str, Any]) -> SessionRecord:
    payload = dict(data)
    for key in ("created_at", "updated_at", "last_activity_at", "ended_at"):
        if key in payload:
            payload[key] = _parse_dt(payload[key])
    return SessionRecord(**payload)


class InMemoryStore:
    """Thread-safe dict store for tests and local mode."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> SessionRecord | None:
        with self._lock:
            raw = self._items.get(session_id)
            if raw is None:
                return None
            raw = dict(raw)
        return record_from_dict(raw)

    def put(self, record: SessionRecord) -> None:
        with self._lock:
            self._items[record.id] = record_to_dict(record)

    def list_all(self) -> list[SessionRecord]:
        with self._lock:
            values = list(self._items.values())
        return [record_from_dict(raw) for raw in values]

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._items.pop(session_id, None)


class ModalDictStore:
    """Production store backed by ``modal.Dict``. Lazy-imports modal."""

    def __init__(self, name: str = SESSIONS_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    def get(self, session_id: str) -> SessionRecord | None:
        raw = self._d().get(session_id)
        if raw is None:
            return None
        return record_from_dict(raw)

    def put(self, record: SessionRecord) -> None:
        self._d().put(record.id, record_to_dict(record))

    def list_all(self) -> list[SessionRecord]:
        out: list[SessionRecord] = []
        items: Iterator[tuple[Any, Any]] = self._d().items()
        for _key, raw in items:
            if isinstance(raw, dict):
                out.append(record_from_dict(raw))
        return out

    def delete(self, session_id: str) -> None:
        try:
            self._d().pop(session_id)
        except KeyError:
            return
