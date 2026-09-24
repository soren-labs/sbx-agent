"""Session persistence: Protocol + InMemoryStore (tests) + ModalDictStore (prod)."""

from __future__ import annotations

import copy
import os
import threading
import time
import uuid
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


_DICT_FANOUT = 32

# SOR-199: upper bound on cross-container staleness for the read-through
# listing cache. A write whose ``ver`` bump is lost mid-crash leaves
# remote readers on their cached page only until this TTL expires.
_LIST_CACHE_TTL_S = float(os.environ.get("SBX_LIST_CACHE_TTL_S", "30"))


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

    Listing cache: even the indexed listing costs ~N/fanout serial
    round-trips (25+ at production scale), which put the pooled P95 over
    budget on stragglers and cron overlap. ``put``/``delete`` therefore
    bump an opaque ``ver`` token in the index Dict *after* the record
    write, and ``list_all`` reads ``ver`` first: an unchanged token
    serves the in-process page with a single round-trip. The token is
    read *before* the manifest and records so a write landing mid-fetch
    can only make the cached page stale-early (next call refetches),
    never stale-late. A lost ``ver`` bump (crash between record write
    and token write) is bounded by ``_LIST_CACHE_TTL_S``.
    """

    _IDX_IDS = "agents"
    _IDX_BUILT = "built"
    _IDX_VER = "ver"

    def __init__(self, name: str = SESSIONS_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None
        self._index: Any = None
        self._index_ready = False
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        # ``(ver, monotonic-ts, raw record dicts)`` for the last fetch.
        self._list_cache: tuple[Any, float, list[dict[str, Any]]] | None = None

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
        self._index_bump_ver()

    def list_all(self) -> list[SessionRecord]:
        raws: list[Any] | None = None
        try:
            self._ensure_index()
            # Read the version token first: a write that lands anywhere
            # after this point moves ``ver`` past what we cache under, so
            # the next call refetches instead of serving stale records.
            ver = self._idx().get(self._IDX_VER)
            cached = self._list_cache
            if (
                cached is not None
                and cached[0] == ver
                and time.monotonic() - cached[1] < _LIST_CACHE_TTL_S
            ):
                return [record_from_dict(copy.deepcopy(raw)) for raw in cached[2]]
            ids = self._idx().get(self._IDX_IDS) or []
            ids = sorted({str(i) for i in ids})
            with observe("modal_dict.get_records", store=self._name, records=len(ids)):
                with ThreadPoolExecutor(max_workers=_DICT_FANOUT) as pool:
                    raws = [r for r in pool.map(self._d().get, ids) if isinstance(r, dict)]
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
        self._list_cache = (ver, time.monotonic(), raws)
        return [record_from_dict(copy.deepcopy(raw)) for raw in raws]

    def delete(self, session_id: str) -> None:
        try:
            with observe("modal_dict.pop", store=self._name, key=session_id):
                self._d().pop(session_id)
        except KeyError:
            pass
        self._index_remove(session_id)
        self._index_bump_ver()

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

    def _index_bump_ver(self) -> None:
        """Advance the listing-version token after a mutation so cached
        pages in every container refetch. Failure degrades to the same
        rebuild path as index maintenance."""
        try:
            self._idx().put(self._IDX_VER, uuid.uuid4().hex)
        except Exception:
            self._index_broken()

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
            elif self._idx().get(self._IDX_VER) is None:
                # Indexed by a pre-cache deploy: mint the token once so
                # the listing cache can key on it.
                self._index_bump_ver()
            # A failed bump drops the marker and clears the flag —
            # re-verify both keys rather than trusting maintenance.
            self._index_ready = (
                self._idx().get(self._IDX_BUILT) is not None
                and self._idx().get(self._IDX_VER) is not None
            )

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
            self._idx().put(self._IDX_VER, uuid.uuid4().hex)
        self._index_ready = True
        return len(keys)
