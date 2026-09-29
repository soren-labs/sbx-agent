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


def _cached_records(
    cached: tuple[Any, float, list[dict[str, Any]]],
) -> list[SessionRecord]:
    return [record_from_dict(copy.deepcopy(raw)) for raw in cached[2]]


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
# remote readers on their cached page only until this TTL expires plus
# one background revalidation.
_LIST_CACHE_TTL_S = float(os.environ.get("SBX_LIST_CACHE_TTL_S", "30"))

# Dict read failures (transient Modal stragglers/outages) are masked by
# the last good page only while it is still reasonably fresh; beyond this
# bound an outage surfaces as the honest enumeration path again.
_LIST_CACHE_STALE_S = max(10 * _LIST_CACHE_TTL_S, 120.0)

# Sentinel for "the version token could not be read" — distinct from a
# stored ``None`` so a failed token read never looks like a write.
_VER_UNREAD: Any = object()

# SOR-268: per-record read-through TTL for ``get``. The V2 read paths
# (settle → ``plane.get`` → ``live_extras``) re-read the same session
# record several times per request and the SSE hub polls it — the cache
# collapses those to ~one remote get per record per window. ``put`` is
# write-through, so the only staleness is a cross-container writer's
# ``put`` — bounded by this TTL, a strictly smaller staleness than the
# listing cache above already accepts.
_GET_CACHE_TTL_S = float(os.environ.get("SBX_SESSION_GET_CACHE_TTL_S", "0.75"))

# ``_index_add`` result sentinel: the id was already indexed, so the index
# write was skipped and the caller must bump ``ver`` itself.
_IDX_WRITE_SKIPPED: Any = object()


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
        # Background revalidation: a TTL-expired page is served stale
        # while one refresh thread repopulates it — callers never pay
        # the full fanout for a cache that merely aged out.
        self._refresh_lock = threading.Lock()
        self._refresh_thread: threading.Thread | None = None
        # SOR-268 per-record read-through cache for ``get``.
        self._get_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        # Ids confirmed present in the manifest — a warm ``put`` skips
        # the index-doc read entirely (record put + ``ver`` bump only).
        self._indexed_ids: set[str] = set()

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
        with self._lock:
            cached = self._get_cache.get(session_id)
        if cached is not None and time.monotonic() - cached[0] < _GET_CACHE_TTL_S:
            raw = cached[1]
        else:
            raw = self._get_uncached(session_id)
        if raw is None:
            return None
        return record_from_dict(raw)

    def _get_uncached(self, session_id: str) -> dict[str, Any] | None:
        with observe("modal_dict.get", store=self._name, key=session_id):
            raw = self._d().get(session_id)
        with self._lock:
            self._get_cache[session_id] = (time.monotonic(), raw if isinstance(raw, dict) else None)
        return raw if isinstance(raw, dict) else None

    def get_fresh(self, session_id: str) -> SessionRecord | None:
        """Uncached point read for mutation paths (the plane's
        read-modify-write under ``_lock`` must not run on a cached row)."""
        raw = self._get_uncached(session_id)
        return record_from_dict(raw) if raw is not None else None

    def put(self, record: SessionRecord) -> None:
        raw = record_to_dict(record)
        ver = self._index_add(record.id)
        with observe("modal_dict.put", store=self._name, key=record.id):
            self._d().put(record.id, raw)
        if ver is _IDX_WRITE_SKIPPED:
            ver = self._index_bump_ver()
        with self._lock:
            self._get_cache[record.id] = (time.monotonic(), raw)
        self._cache_merge(raw, ver)

    def list_all(self) -> list[SessionRecord]:
        cached = self._list_cache
        ver: Any = _VER_UNREAD
        try:
            self._ensure_index()
            # Read the version token first: a write that lands anywhere
            # after this point moves ``ver`` past what we cache under, so
            # the next call refetches instead of serving stale records.
            ver = self._idx().get(self._IDX_VER)
        except Exception:
            pass
        if cached is not None:
            age = time.monotonic() - cached[1]
            if ver is _VER_UNREAD:
                # The token itself is unreadable (transient Dict trouble):
                # serve the last good page while it is still fresh enough
                # and let a background refresh retry the read, rather
                # than streaming ``items()`` behind a straggler.
                if age < _LIST_CACHE_STALE_S:
                    self._kick_refresh()
                    return _cached_records(cached)
            elif cached[0] == ver:
                if age < _LIST_CACHE_TTL_S:
                    return _cached_records(cached)
                # Past TTL with no confirmed write: serve stale and
                # revalidate in the background — a lost ``ver`` bump is
                # still repaired within ~TTL + one refetch, but callers
                # no longer block on the full fanout every TTL window.
                self._kick_refresh()
                return _cached_records(cached)
            # else: a confirmed write moved ``ver`` — refetch below.
        try:
            return self._fetch_indexed(None if ver is _VER_UNREAD else ver)
        except Exception:
            if cached is not None and time.monotonic() - cached[1] < _LIST_CACHE_STALE_S:
                self._kick_refresh()
                return _cached_records(cached)
            # Index unavailable: fall back to the honest full enumeration
            # rather than fail the listing.
            return self._list_scan_fallback()

    def _fetch_indexed(self, ver: Any) -> list[SessionRecord]:
        """Manifest + point-get fetch; repopulates the listing cache."""
        ids = self._idx().get(self._IDX_IDS) or []
        ids = sorted({str(i) for i in ids})
        with observe("modal_dict.get_records", store=self._name, records=len(ids)):
            with ThreadPoolExecutor(max_workers=_DICT_FANOUT) as pool:
                raws = [r for r in pool.map(self._d().get, ids) if isinstance(r, dict)]
        # Stale ids (record deleted between index write and read) fetch
        # None and are skipped.
        self._list_cache = (ver, time.monotonic(), raws)
        with self._lock:
            self._indexed_ids = {str(r.get("id")) for r in raws}
        return [record_from_dict(copy.deepcopy(raw)) for raw in raws]

    def _list_scan_fallback(self) -> list[SessionRecord]:
        out: list[SessionRecord] = []
        with observe("modal_dict.items", store=self._name):
            items: Iterator[tuple[Any, Any]] = self._d().items()
            for _key, raw in items:
                if isinstance(raw, dict):
                    out.append(record_from_dict(raw))
        return out

    def _kick_refresh(self) -> None:
        """Repopulate the listing cache off the request path; deduped by
        ``_refresh_lock`` so stacked stale reads share one refetch."""
        if not self._refresh_lock.acquire(blocking=False):
            return
        self._refresh_thread = threading.Thread(
            target=self._refresh_listing,
            name=f"{self._name}-list-refresh",
            daemon=True,
        )
        self._refresh_thread.start()

    def _refresh_listing(self) -> None:
        try:
            self._ensure_index()
            ver = self._idx().get(self._IDX_VER)
            self._fetch_indexed(ver)
        except Exception:
            pass
        finally:
            self._refresh_lock.release()

    def _cache_merge(self, raw: dict[str, Any], ver: Any) -> None:
        """Fold an in-process ``put`` into the warm page: same-container
        reads stay fresh *and* fast instead of paying a refetch for a
        write this process already knows about. Cross-container writes
        still invalidate through the ``ver`` mismatch."""
        with self._lock:
            cached = self._list_cache
            if cached is None:
                return
            raws = [r for r in cached[2] if r.get("id") != raw.get("id")]
            raws.append(raw)
            raws.sort(key=lambda r: str(r.get("id")))
            self._list_cache = (
                ver if ver is not None else cached[0],
                time.monotonic(),
                raws,
            )

    def _cache_merge_delete(self, session_id: str, ver: Any) -> None:
        with self._lock:
            cached = self._list_cache
            if cached is None:
                return
            raws = [r for r in cached[2] if r.get("id") != session_id]
            self._list_cache = (
                ver if ver is not None else cached[0],
                time.monotonic(),
                raws,
            )

    def delete(self, session_id: str) -> None:
        try:
            with observe("modal_dict.pop", store=self._name, key=session_id):
                self._d().pop(session_id)
        except KeyError:
            pass
        self._index_remove(session_id)
        ver = self._index_bump_ver()
        with self._lock:
            self._get_cache.pop(session_id, None)
        self._cache_merge_delete(session_id, ver)

    # ------------------------------------------------------- listing index

    def _index_add(self, session_id: str) -> Any:
        """Ensure ``session_id`` is in the id manifest. Returns the minted
        ``ver`` token when the index update batched it (the caller then
        skips the separate bump), ``_IDX_WRITE_SKIPPED`` when the id was
        already indexed (caller bumps ``ver`` itself)."""
        with self._lock:
            if session_id in self._indexed_ids:
                return _IDX_WRITE_SKIPPED
        try:
            with self._lock:
                if session_id in self._indexed_ids:
                    return _IDX_WRITE_SKIPPED
                ids = self._idx().get(self._IDX_IDS) or []
                if session_id in ids:
                    self._indexed_ids.add(session_id)
                    return _IDX_WRITE_SKIPPED
                token = uuid.uuid4().hex
                self._idx_update({self._IDX_IDS: sorted([*ids, session_id]), self._IDX_VER: token})
                self._indexed_ids.add(session_id)
                return token
        except Exception:
            self._index_broken()
            return _IDX_WRITE_SKIPPED

    def _idx_update(self, writes: dict[str, Any]) -> None:
        """One-RPC multi-key index write via ``Dict.update``; older
        clients degrade to per-key puts."""
        update = getattr(self._idx(), "update", None)
        if callable(update):
            update(writes)
            return
        for key, value in writes.items():
            self._idx().put(key, value)

    def _index_remove(self, session_id: str) -> None:
        try:
            with self._lock:
                ids = self._idx().get(self._IDX_IDS) or []
                if session_id not in ids:
                    return
                self._idx().put(self._IDX_IDS, [i for i in ids if i != session_id])
                self._indexed_ids.discard(session_id)
        except Exception:
            self._index_broken()

    def _index_broken(self) -> None:
        """Index maintenance failed: drop the marker so the next listing
        rebuilds and converges instead of serving a drifted manifest."""
        self._index_ready = False
        with self._lock:
            self._indexed_ids.clear()
        try:
            self._idx().pop(self._IDX_BUILT)
        except Exception:
            pass

    def _index_bump_ver(self) -> Any:
        """Advance the listing-version token after a mutation so cached
        pages in every container refetch. Returns the minted token so the
        writing process can fold its own change into its cache; failure
        degrades to the same rebuild path as index maintenance."""
        try:
            token = uuid.uuid4().hex
            self._idx().put(self._IDX_VER, token)
            return token
        except Exception:
            self._index_broken()
            return None

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
            self._indexed_ids = set(keys)
        self._index_ready = True
        return len(keys)
