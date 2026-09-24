"""Durable workflow/task metadata index (SOR-84 C1 / SOR-91).

Callers tag ``POST /v1/agents`` creates with ``metadata`` —
``workflow_id`` / ``task_id`` / ``role`` / ``parent_task_id?`` — and this
module persists the ``(owner, workflow_id) → tasks`` relationship so a
fresh control-plane process can recover a workflow from nothing but the
API key id + ``workflow_id``. ``owner`` is ``SessionRecord.owner`` (the api
key id); it is part of every lookup key so two keys can reuse a
``workflow_id`` without ever seeing — or cleaning up — each other's
agents.

Layout mirrors ``control.run_store``: a ``WorkflowStore`` Protocol with an
in-memory implementation for tests, a JSON-file implementation for local
durability, and a ``modal.Dict``-backed implementation for production.
Each store writes two records per attach:

* an ``agent`` record — ``agent_id → WorkflowTaskRecord`` for per-agent
  reverse lookup;
* an ``index`` record — ``(owner, workflow_id) → {agent_id: record}`` so
  bindings whose per-agent record was lost or corrupted are still
  recoverable from the workflow entry alone.

``list_workflow`` merges the index with a scan of the per-agent records —
which are authoritative — so a missing, corrupt, or valid-but-stale index
(crash mid-attach, lost update) can never hide a live agent from scoped
cleanup or double-list one that moved workflows.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

from control.config import WORKFLOWS_DICT_NAME
from control.latency import observe

_DICT_FANOUT = 8


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _key_part(value: str) -> str:
    """Filesystem-safe encoding for caller-controlled id segments."""
    return quote(value, safe="")


@dataclass
class WorkflowTaskRecord:
    """One agent's binding into a caller-defined workflow task."""

    owner: str
    workflow_id: str
    task_id: str
    role: str
    agent_id: str
    parent_task_id: str | None = None
    created_at: str = ""
    updated_at: str = ""


def record_to_dict(record: WorkflowTaskRecord) -> dict[str, Any]:
    return asdict(record)


def record_from_dict(data: Any) -> WorkflowTaskRecord:
    """Strict-ish decode; raises ``ValueError`` on unusable payloads."""
    if not isinstance(data, dict):
        raise ValueError("workflow task record is not a dict")
    fields: dict[str, str] = {}
    for key in ("owner", "workflow_id", "task_id", "role", "agent_id"):
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"workflow task record missing {key}")
        fields[key] = value
    parent = data.get("parent_task_id")
    if parent is not None and not isinstance(parent, str):
        raise ValueError("workflow task record parent_task_id must be a string")
    record = WorkflowTaskRecord(**fields, parent_task_id=parent)
    for key in ("created_at", "updated_at"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"workflow task record field {key} must be a string")
        setattr(record, key, value or "")
    return record


def _decode(raw: Any) -> WorkflowTaskRecord | None:
    """Decode a stored payload; corrupt entries are skipped, not fatal."""
    try:
        return record_from_dict(raw)
    except (ValueError, TypeError):
        return None


def _index_put(
    index: dict[str, Any] | None, record: WorkflowTaskRecord, now: str
) -> dict[str, Any]:
    """Index payload for ``(owner, workflow_id)`` with ``record`` merged in."""
    index = index or {}
    tasks = dict(index.get("tasks") or {})
    tasks[record.agent_id] = record_to_dict(record)
    return {
        "owner": record.owner,
        "workflow_id": record.workflow_id,
        "tasks": tasks,
        "created_at": index.get("created_at") or now,
        "updated_at": now,
    }


def _index_records(index: dict[str, Any] | None) -> list[WorkflowTaskRecord]:
    """Records embedded in an index payload, sorted by (created_at, agent)."""
    tasks = (index or {}).get("tasks") or {}
    records = [r for r in (_decode(raw) for raw in tasks.values()) if r is not None]
    return sorted(records, key=lambda r: (r.created_at, r.agent_id))


@runtime_checkable
class WorkflowStore(Protocol):
    """Persistence for ``WorkflowTaskRecord`` plus the workflow index."""

    def attach(self, record: WorkflowTaskRecord) -> WorkflowTaskRecord:
        """Idempotent upsert keyed by ``agent_id``.

        Re-attaching an agent under a different ``(owner, workflow_id)``
        moves its index entry; ``created_at`` is preserved across
        re-attaches.
        """

    def for_agent(self, agent_id: str) -> WorkflowTaskRecord | None:
        """The task binding for one agent, or None."""

    def all_bindings(self) -> dict[str, WorkflowTaskRecord]:
        """Every decodable per-agent binding in one backing-store pass.

        ``GET /v1/agents`` resolves the ``metadata`` echo for a whole
        page; serial per-agent gets would turn every list into an N+1 of
        remote reads (SOR-200). Agents with no decodable binding are
        absent from the map.
        """

    def list_workflow(self, owner: str, workflow_id: str) -> list[WorkflowTaskRecord]:
        """All task records of ``(owner, workflow_id)``, created_at order.

        The index entry is merged with a scan of the per-agent records —
        which are authoritative — so even a valid-but-stale index (a crash
        between the agent write and the index write, or a lost update on a
        backend without read-modify-write) can never hide a live agent
        from scoped cleanup.
        """


class _WorkflowStoreBase:
    """Shared attach/list logic over per-backend lock-free primitives.

    Public methods serialize on ``self._lock``; the ``_*_raw`` primitives
    are plain reads/writes used inside that lock (and by the lock-free
    reads ``for_agent``/``all_bindings``/``list_workflow``).
    """

    _lock: threading.RLock

    def _get_agent_raw(self, agent_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def _put_agent_raw(self, record: WorkflowTaskRecord) -> None:
        raise NotImplementedError

    def _get_index_raw(self, owner: str, workflow_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def _put_index_raw(self, owner: str, workflow_id: str, index: dict[str, Any]) -> None:
        raise NotImplementedError

    def _iter_agent_raws(self) -> Iterator[dict[str, Any]]:
        raise NotImplementedError

    def attach(self, record: WorkflowTaskRecord) -> WorkflowTaskRecord:
        now = _iso_now()
        with self._lock:
            prior = _decode(self._get_agent_raw(record.agent_id))
            if prior is not None:
                if (prior.owner, prior.workflow_id) != (record.owner, record.workflow_id):
                    # Moved: drop the stale slot from the old workflow's
                    # index so the agent is never counted/closed twice.
                    old = self._get_index_raw(prior.owner, prior.workflow_id)
                    if old is not None:
                        tasks = dict(old.get("tasks") or {})
                        tasks.pop(record.agent_id, None)
                        self._put_index_raw(
                            prior.owner,
                            prior.workflow_id,
                            {**old, "tasks": tasks, "updated_at": now},
                        )
                record.created_at = prior.created_at
            record.created_at = record.created_at or now
            record.updated_at = now
            self._put_agent_raw(record)
            index = self._get_index_raw(record.owner, record.workflow_id)
            self._put_index_raw(
                record.owner,
                record.workflow_id,
                _index_put(index, record, now),
            )
        return record

    def for_agent(self, agent_id: str) -> WorkflowTaskRecord | None:
        return _decode(self._get_agent_raw(agent_id))

    def all_bindings(self) -> dict[str, WorkflowTaskRecord]:
        """Batch ``for_agent``: one scan, same decode-and-skip rules.

        On the ``modal.Dict`` backend this costs a single ``items()`` call
        for any page size instead of one ``get`` round-trip per agent.
        """
        out: dict[str, WorkflowTaskRecord] = {}
        try:
            raws = self._iter_agent_raws()
        except Exception:
            return out
        for raw in raws:
            record = _decode(raw)
            if record is not None:
                out[record.agent_id] = record
        return out

    def list_workflow(self, owner: str, workflow_id: str) -> list[WorkflowTaskRecord]:
        by_agent: dict[str, WorkflowTaskRecord] = {}
        # Per-agent records are authoritative. The scan runs first so a
        # stale index slot can neither hide a live agent (a crash between
        # the agent write and the index write, or a lost update on a
        # backend without read-modify-write) nor double-list one whose
        # binding moved to another workflow (a lost index removal).
        known: set[str] = set()
        try:
            for record in (_decode(raw) for raw in self._iter_agent_raws()):
                if record is None:
                    continue
                known.add(record.agent_id)
                if record.owner == owner and record.workflow_id == workflow_id:
                    by_agent[record.agent_id] = record
        except Exception:
            # Scan failed: index entries stand alone rather than the whole
            # listing disappearing.
            known = set()
        index = self._get_index_raw(owner, workflow_id)
        if isinstance(index, dict) and isinstance(index.get("tasks"), dict):
            for record in _index_records(index):
                if record.agent_id not in known and record.agent_id not in by_agent:
                    by_agent[record.agent_id] = record
        return sorted(by_agent.values(), key=lambda r: (r.created_at, r.agent_id))


class InMemoryWorkflowStore(_WorkflowStoreBase):
    """Thread-safe dict store for tests and ephemeral deployments."""

    def __init__(self) -> None:
        self._agents: dict[str, dict[str, Any]] = {}
        self._indexes: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()

    def _get_agent_raw(self, agent_id: str) -> dict[str, Any] | None:
        raw = self._agents.get(agent_id)
        return dict(raw) if raw is not None else None

    def _put_agent_raw(self, record: WorkflowTaskRecord) -> None:
        self._agents[record.agent_id] = record_to_dict(record)

    def _get_index_raw(self, owner: str, workflow_id: str) -> dict[str, Any] | None:
        raw = self._indexes.get((owner, workflow_id))
        return dict(raw) if raw is not None else None

    def _put_index_raw(self, owner: str, workflow_id: str, index: dict[str, Any]) -> None:
        self._indexes[(owner, workflow_id)] = dict(index)

    def _iter_agent_raws(self) -> Iterator[dict[str, Any]]:
        with self._lock:
            return iter([dict(raw) for raw in self._agents.values()])


class FileWorkflowStore(_WorkflowStoreBase):
    """Local durable store under ``root``.

    ``agents/<agent_id>.json`` holds each task record; the index lives at
    ``index/<owner>/<workflow_id>.json`` (id segments are percent-encoded).
    Writes are atomic (tmp file + rename) so a crash mid-write cannot leave
    a half-written record.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root

    def _agent_path(self, agent_id: str) -> Path:
        return self._root / "agents" / f"{_key_part(agent_id)}.json"

    def _index_path(self, owner: str, workflow_id: str) -> Path:
        return self._root / "index" / _key_part(owner) / f"{_key_part(workflow_id)}.json"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)

    def _get_agent_raw(self, agent_id: str) -> dict[str, Any] | None:
        return self._read_json(self._agent_path(agent_id))

    def _put_agent_raw(self, record: WorkflowTaskRecord) -> None:
        self._write_json(self._agent_path(record.agent_id), record_to_dict(record))

    def _get_index_raw(self, owner: str, workflow_id: str) -> dict[str, Any] | None:
        return self._read_json(self._index_path(owner, workflow_id))

    def _put_index_raw(self, owner: str, workflow_id: str, index: dict[str, Any]) -> None:
        self._write_json(self._index_path(owner, workflow_id), index)

    def _iter_agent_raws(self) -> Iterator[dict[str, Any]]:
        try:
            paths = sorted((self._root / "agents").glob("*.json"))
        except OSError:
            return iter(())
        return (raw for raw in (self._read_json(p) for p in paths) if raw is not None)


class ModalDictWorkflowStore(_WorkflowStoreBase):
    """Production store backed by ``modal.Dict``. Lazy-imports modal.

    Listing manifest (SOR-199): ``wf-index/_agents`` -> sorted bound-agent
    id list. ``modal.Dict`` enumeration is server-paged at ~one round-trip
    per key, so ``all_bindings``/``list_workflow`` read the manifest and
    fetch agent records point-wise through a bounded pool instead of
    ``items()`` over the Dict. The manifest is written *before* the agent
    record so a crash between the pair leaves at most a stale id —
    skipped on read — never an invisible binding; per-agent records stay
    authoritative (the merge semantics of ``list_workflow`` are
    unchanged). A missing/corrupt manifest is rebuilt once from
    ``keys()`` — the lazy migration for pre-manifest Dicts.
    """

    _AGENT_PREFIX = "wf-agent/"
    _INDEX_PREFIX = "wf-index/"
    _AGENTS_MANIFEST = "wf-index/_agents"

    def __init__(self, name: str = WORKFLOWS_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None
        self._lock = threading.RLock()
        self._manifest_ready = False
        self._build_lock = threading.Lock()

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    def _get_agent_raw(self, agent_id: str) -> dict[str, Any] | None:
        key = f"{self._AGENT_PREFIX}{agent_id}"
        with observe("modal_dict.get", store=self._name, key=key):
            raw = self._d().get(key)
        return raw if isinstance(raw, dict) else None

    def _put_agent_raw(self, record: WorkflowTaskRecord) -> None:
        self._manifest_add(record.agent_id)
        key = f"{self._AGENT_PREFIX}{record.agent_id}"
        with observe("modal_dict.put", store=self._name, key=key):
            self._d().put(key, record_to_dict(record))

    def _get_index_raw(self, owner: str, workflow_id: str) -> dict[str, Any] | None:
        key = f"{self._INDEX_PREFIX}{owner}/{workflow_id}"
        with observe("modal_dict.get", store=self._name, key=key):
            raw = self._d().get(key)
        return raw if isinstance(raw, dict) else None

    def _put_index_raw(self, owner: str, workflow_id: str, index: dict[str, Any]) -> None:
        key = f"{self._INDEX_PREFIX}{owner}/{workflow_id}"
        with observe("modal_dict.put", store=self._name, key=key):
            self._d().put(key, index)

    def _iter_agent_raws(self) -> Iterator[dict[str, Any]]:
        try:
            ids = self._manifest_ids()
            with observe("modal_dict.get_agent_raws", store=self._name, agents=len(ids)):
                with ThreadPoolExecutor(max_workers=_DICT_FANOUT) as pool:
                    raws = list(
                        pool.map(lambda aid: self._d().get(f"{self._AGENT_PREFIX}{aid}"), ids)
                    )
            # Stale ids fetch None and are skipped; agent records stay
            # authoritative.
            return iter([raw for raw in raws if isinstance(raw, dict)])
        except Exception:
            # Manifest path failed — the honest full enumeration keeps the
            # listing available.
            with observe("modal_dict.items", store=self._name):
                items = list(self._d().items())
            return (
                raw
                for key, raw in items
                if isinstance(key, str)
                and key.startswith(self._AGENT_PREFIX)
                and isinstance(raw, dict)
            )

    # ---------------------------------------------------- listing manifest

    def _manifest_add(self, agent_id: str) -> None:
        try:
            with self._lock:
                ids = self._d().get(self._AGENTS_MANIFEST) or []
                if not isinstance(ids, list):
                    ids = []
                if agent_id in ids:
                    return
                self._d().put(self._AGENTS_MANIFEST, sorted({*ids, agent_id}))
        except Exception:
            # Manifest maintenance must never fail the binding write;
            # dropping the manifest forces the next scan to rebuild it.
            self._manifest_ready = False
            try:
                self._d().pop(self._AGENTS_MANIFEST)
            except Exception:
                pass

    def _manifest_ids(self) -> list[str]:
        """Sorted bound-agent ids, rebuilding the manifest once when absent.
        ``_build_lock`` double-checks so concurrent first scans share one
        rebuild."""
        if not self._manifest_ready:
            with self._build_lock:
                if not self._manifest_ready:
                    raw = self._d().get(self._AGENTS_MANIFEST)
                    if not isinstance(raw, list):
                        self.rebuild_manifest()
                    self._manifest_ready = True
        raw = self._d().get(self._AGENTS_MANIFEST) or []
        return sorted({str(i) for i in raw}) if isinstance(raw, list) else []

    def rebuild_manifest(self) -> int:
        """Re-enumerate ``wf-agent/`` keys once and rewrite the manifest.

        Idempotent and safe on a live store; returns the number of ids
        indexed.
        """
        with observe("modal_dict.keys", store=self._name):
            keys = [k for k in self._d().keys() if isinstance(k, str)]
        ids = sorted(k[len(self._AGENT_PREFIX) :] for k in keys if k.startswith(self._AGENT_PREFIX))
        with self._lock:
            self._d().put(self._AGENTS_MANIFEST, ids)
        self._manifest_ready = True
        return len(ids)


__all__ = [
    "FileWorkflowStore",
    "InMemoryWorkflowStore",
    "ModalDictWorkflowStore",
    "WorkflowStore",
    "WorkflowTaskRecord",
    "record_from_dict",
    "record_to_dict",
]
