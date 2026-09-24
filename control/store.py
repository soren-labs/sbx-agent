"""Session persistence: Protocol + InMemoryStore (tests) + ModalDictStore (prod)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from control.backend import SandboxHandle
from control.config import SESSIONS_DICT_NAME
from control.latency import observe


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
    # SOR-181: resolved per-agent compute spec
    # (``{"cpu": [min, max], "memory_mib": [min, max]}``) — durable so the
    # declared sizing survives restarts and recovery, feeds the status
    # echo, and makes the cost estimate compute-aware.
    compute: dict[str, Any] | None = None
    # SOR-179: canonical reasoning effort declared at create; every run of
    # the agent inherits it (provider CLI sees it via ``runner init``).
    reasoning_effort: str | None = None
    # SOR-180: Secret *refs* the sandbox was provisioned with
    # (``{"secrets": [...], "resource_secrets": [...]}``, names only —
    # never values). A checkpoint restore re-declares them on the fresh
    # sandbox so it mounts the same credential/resource channels.
    spec_secrets: dict[str, Any] | None = None

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


_DICT_FANOUT = 8


class ModalDictStore:
    """Production store backed by ``modal.Dict``. Lazy-imports modal.

    Listing index (SOR-199): a companion ``<name>-index`` Dict holds
    ``agents`` -> sorted session-id list and ``built`` once the index
    covers the whole store. ``modal.Dict`` enumeration is server-paged at
    ~one round-trip per key, so ``list_all`` reads the id manifest and
    fetches records point-wise through a bounded pool instead of
    ``items()`` over the Dict. ``put`` writes the index id *before* the
    record and ``delete`` removes the record *before* the id, so a crash
    between the pair leaves at most a stale id — skipped on read — never
    an invisible live record. A lost index update is repaired by
    ``rebuild_index`` (also the lazy migration for pre-index Dicts).
    """

    _IDX_IDS = "agents"
    _IDX_BUILT = "built"

    def __init__(self, name: str = SESSIONS_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None
        self._index: Any = None
        self._index_ready = False
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    def _idx(self) -> Any:
        if self._index is None:
            import modal

            self._index = modal.Dict.from_name(f"{self._name}-index", create_if_missing=True)
        return self._index

    def get(self, session_id: str) -> SessionRecord | None:
        with observe("modal_dict.get", store=self._name, key=session_id):
            raw = self._d().get(session_id)
        if raw is None:
            return None
        return record_from_dict(raw)

    def put(self, record: SessionRecord) -> None:
        self._index_add(record.id)
        with observe("modal_dict.put", store=self._name, key=record.id):
            self._d().put(record.id, record_to_dict(record))

    def list_all(self) -> list[SessionRecord]:
        raws: list[Any] | None = None
        try:
            self._ensure_index()
            ids = self._idx().get(self._IDX_IDS) or []
            ids = sorted({str(i) for i in ids})
            with observe("modal_dict.get_records", store=self._name, records=len(ids)):
                with ThreadPoolExecutor(max_workers=_DICT_FANOUT) as pool:
                    raws = list(pool.map(self._d().get, ids))
        except Exception:
            raws = None
        if raws is None:
            # Index unavailable: fall back to the honest full enumeration
            # rather than fail the listing.
            out: list[SessionRecord] = []
            with observe("modal_dict.items", store=self._name):
                items: Iterator[tuple[Any, Any]] = self._d().items()
                for _key, raw in items:
                    if isinstance(raw, dict):
                        out.append(record_from_dict(raw))
            return out
        # Stale ids (record deleted between index write and read) fetch
        # None and are skipped.
        return [record_from_dict(raw) for raw in raws if isinstance(raw, dict)]

    def delete(self, session_id: str) -> None:
        try:
            with observe("modal_dict.pop", store=self._name, key=session_id):
                self._d().pop(session_id)
        except KeyError:
            pass
        self._index_remove(session_id)

    # ------------------------------------------------------- listing index

    def _index_add(self, session_id: str) -> None:
        try:
            with self._lock:
                ids = self._idx().get(self._IDX_IDS) or []
                if session_id in ids:
                    return
                self._idx().put(self._IDX_IDS, sorted([*ids, session_id]))
        except Exception:
            self._index_broken()

    def _index_remove(self, session_id: str) -> None:
        try:
            with self._lock:
                ids = self._idx().get(self._IDX_IDS) or []
                if session_id not in ids:
                    return
                self._idx().put(self._IDX_IDS, [i for i in ids if i != session_id])
        except Exception:
            self._index_broken()

    def _index_broken(self) -> None:
        """Index maintenance failed: drop the marker so the next listing
        rebuilds and converges instead of serving a drifted manifest."""
        self._index_ready = False
        try:
            self._idx().pop(self._IDX_BUILT)
        except Exception:
            pass

    def _ensure_index(self) -> None:
        """Lazily build the id manifest on the first listing — the safe
        migration for Dicts that predate the index. ``_build_lock``
        double-checks so concurrent first listings share one rebuild."""
        if self._index_ready:
            return
        with self._build_lock:
            if self._index_ready:
                return
            if self._idx().get(self._IDX_BUILT) is None:
                self.rebuild_index()
            self._index_ready = True

    def rebuild_index(self) -> int:
        """Re-enumerate keys once and rewrite the id manifest.

        Idempotent and safe on a live store; returns the number of ids
        indexed.
        """
        with observe("modal_dict.keys", store=self._name):
            keys = sorted(k for k in self._d().keys() if isinstance(k, str))
        with self._lock:
            self._idx().put(self._IDX_IDS, keys)
            self._idx().put(self._IDX_BUILT, b"1")
        self._index_ready = True
        return len(keys)
