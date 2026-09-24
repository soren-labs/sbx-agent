"""Durable run ledger (SOR-82/A1).

Run state is persisted at transition time instead of being re-derived from
sandbox files on every GET. A terminal record (``FINISHED`` / ``ERROR`` /
``CANCELLED`` / ``EXPIRED``) is monotonic: once persisted it never changes,
so sandbox teardown or a control-plane restart cannot rewrite history.

Missing or corrupt records surface as an explicit ``UNKNOWN`` status with an
``error`` payload — never as inferred success.

Layout mirrors ``control.store``: a ``RunStore`` Protocol with an in-memory
implementation for tests, a JSON-file implementation for local durability,
and a ``modal.Dict``-backed implementation for production. ``RunLedger`` adds
the transition rules (idempotent ``begin``, monotonic ``finish`` /
``cancel``, ``discard`` for rolled-back turns) on top of any store.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from runtime.runner.contract import (
    _MAX_EVAL_DEPTH,
    STATUS_INVALID,
    STATUS_PENDING,
    STATUS_SKIPPED,
    STATUS_VALID,
    _json_depth,
    evaluate_output,
    violation_summary,
)

from control.config import RUNS_DICT_NAME
from control.latency import observe
from control.run_errors import (
    CODE_CONTRACT_VIOLATION,
    CODE_EVENT_PARSE_ERROR,
    RunError,
    run_error_from_turn,
)

OPEN_RUN_STATUSES = frozenset({"CREATING", "RUNNING"})
TERMINAL_RUN_STATUSES = frozenset({"FINISHED", "ERROR", "CANCELLED", "EXPIRED"})
UNKNOWN_RUN_STATUS = "UNKNOWN"
RUN_STATUSES = OPEN_RUN_STATUSES | TERMINAL_RUN_STATUSES | {UNKNOWN_RUN_STATUS}

_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "cache_write_input_tokens",
    "reasoning_output_tokens",
)


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def run_error(
    code: str,
    message: str,
    *,
    source: str = "runtime",
    retryable: bool = False,
    retry_after: float | None = None,
) -> dict[str, Any]:
    """Structured error persisted on a run record (canonical SOR-82 shape)."""
    return RunError(
        code=code,
        source=source,
        message=message,
        retryable=retryable,
        retry_after=retry_after,
    ).public()


def default_artifact_refs(n: int) -> list[str]:
    """Sandbox-relative evidence paths for turn ``n`` (filesystem.md)."""
    return [
        f"turns/{n}.json",
        f"inbox/{n}.md",
        "events.jsonl",
        "events.raw.jsonl",
    ]


def _usage_from(raw: dict[str, Any]) -> dict[str, int]:
    """Coerce a turn payload's usage dict; unreadable fields are dropped —
    corrupt evidence must never wedge the terminal persist."""
    usage: dict[str, int] = {}
    for key in _USAGE_KEYS:
        if key in raw:
            try:
                usage[key] = int(raw[key])
            except (TypeError, ValueError):
                continue
    return usage


def outcome_from_turn_payload(
    payload: dict[str, Any] | None,
) -> tuple[str, dict[str, Any] | None, str | None, dict[str, int] | None]:
    """Map a ``turns/<n>.json`` payload to (run status, error, result, usage).

    A missing/unreadable payload is explicit ``ERROR`` +
    ``runtime_error`` — never inferred success.
    """
    if not isinstance(payload, dict):
        return (
            "ERROR",
            RunError(
                "runtime_error",
                "runtime",
                "turn outcome missing: turns/<n>.json unreadable after the turn exited",
                retryable=True,
            ).public(),
            None,
            None,
        )
    turn_status = str(payload.get("status") or "")
    message = payload.get("message")
    result_text = str(message) if message else None
    usage_raw = payload.get("usage")
    usage = _usage_from(usage_raw) if isinstance(usage_raw, dict) else None
    if turn_status == "success":
        # Completeness confirmed by the turn record; a recorded parse failure
        # stays visible as an explicit warning on the FINISHED run.
        warning = run_error_from_turn(payload)
        error = (
            warning.public()
            if warning is not None and warning.code == CODE_EVENT_PARSE_ERROR
            else None
        )
        return "FINISHED", error, result_text, usage
    if turn_status == "timeout":
        err = run_error_from_turn(payload) or RunError(
            "timeout", "runtime", "turn exceeded max seconds", retryable=True
        )
        return "EXPIRED", err.public(), result_text, usage
    if turn_status == "cancelled":
        err = run_error_from_turn(payload) or RunError("cancelled", "control", "run cancelled")
        return "CANCELLED", err.public(), result_text, usage
    err = run_error_from_turn(payload)
    if err is None:
        # Contradictory record: turn failed but nothing diagnoses it.
        exit_code = payload.get("exit_code")
        err = RunError(
            "runtime_error",
            "runtime",
            f"turn failed (status={turn_status or 'unknown'} exit_code={exit_code})",
        )
    return "ERROR", err.public(), result_text, usage


def apply_output_contract(
    status: str,
    error: dict[str, Any] | None,
    result_text: str | None,
    contract: dict[str, Any] | None,
) -> tuple[str, dict[str, Any] | None, Any, dict[str, Any] | None]:
    """Enforce a run's output contract at terminal persist (SOR-130).

    ``contract`` is the normalized request (``{schema, enforcement,
    schema_digest}``) persisted on the open record. Returns ``(status,
    error, structured_output, contract_result)`` where ``contract_result``
    is the public ``output_contract`` metadata
    (``{enforcement, schema_digest, status, extraction, violations}``).

    Semantics:

    - Non-FINISHED outcomes keep their status; the contract is ``skipped``
      (there was no evaluable success to judge).
    - ``valid`` keeps FINISHED and persists the extracted JSON value.
    - ``invalid`` under ``strict`` flips the run to ``ERROR`` +
      ``contract_violation`` — machine-diagnosable, never a silent success.
    - ``invalid`` under ``warn`` keeps FINISHED but attaches the same
      diagnostic error, so the violation is explicit either way.

    The verdict is computed here — on the control plane, from the recorded
    message — rather than trusting ``turns/<n>.json.output_contract``, so a
    forged or stale sandbox payload cannot rewrite the decision.
    """
    if contract is None:
        return status, error, None, None
    enforcement = str(contract.get("enforcement") or "strict")
    meta = {
        "enforcement": enforcement,
        "schema_digest": contract.get("schema_digest"),
    }
    if status != "FINISHED":
        return (
            status,
            error,
            None,
            {**meta, "status": STATUS_SKIPPED, "extraction": None, "violations": []},
        )
    try:
        verdict = evaluate_output(result_text or "", contract.get("schema"))
    except Exception as exc:
        # evaluate_output is total; this backstop guarantees the seam that
        # judges untrusted output can never wedge the terminal persist.
        verdict = {
            "status": STATUS_INVALID,
            "value": None,
            "extraction": None,
            "violations": [
                {
                    "path": "$",
                    "code": "evaluation_error",
                    "message": f"evaluation crashed: {type(exc).__name__}",
                }
            ],
        }
    result = {
        **meta,
        "status": verdict["status"],
        "extraction": verdict["extraction"],
        "violations": verdict["violations"],
    }
    if verdict["status"] == STATUS_VALID:
        return status, error, verdict["value"], result
    violation = RunError(
        CODE_CONTRACT_VIOLATION,
        "control",
        f"agent output failed the output contract ({violation_summary(verdict)})",
        retryable=True,
    ).public()
    if enforcement == "warn":
        return status, violation, verdict["value"], result
    return "ERROR", violation, verdict["value"], result


def contract_view(record: RunRecord) -> dict[str, Any] | None:
    """Public ``output_contract`` metadata for a run record (SOR-130).

    ``None`` when the run carries no contract. While the run is still open
    the verdict is ``pending`` — the contract is attached but unevaluated.
    A terminal record that never wrote a verdict (cancelled, or finalized
    before contract fields existed) reports ``skipped`` — never ``pending``
    on a finished run.
    """
    contract = record.output_contract
    if contract is None:
        return None
    result = record.contract_result or {}
    status = result.get("status")
    if status is None:
        status = STATUS_SKIPPED if record.terminal else STATUS_PENDING
    schema = contract.get("schema")
    if not isinstance(schema, dict) or _json_depth(schema) > _MAX_EVAL_DEPTH:
        # A normalized contract is always shallower than the eval bound —
        # anything deeper is a corrupt record; don't echo it into a
        # response it could make unserializable.
        schema = None
    return {
        "schema": schema,
        "enforcement": contract.get("enforcement", "strict"),
        "schema_digest": contract.get("schema_digest"),
        "status": status,
        "extraction": result.get("extraction"),
        "violations": result.get("violations") or [],
    }


@dataclass
class RunRecord:
    """One durable run (``agent ≙ session``, ``run ≙ turn``).

    ``created_at`` / ``updated_at`` are empty strings only on synthesized
    records (corrupt payloads) where the real timestamps are unknown.
    """

    agent_id: str
    n: int
    status: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result_text: str | None = None
    error: dict[str, Any] | None = None
    usage: dict[str, int] | None = None
    provider: str | None = None
    account_id: str | None = None
    model: str | None = None
    # SOR-179: the agent's declared canonical reasoning effort, echoed so
    # every run carries the effective level it executed under.
    reasoning_effort: str | None = None
    artifact_refs: list[str] = field(default_factory=list)
    # SOR-130: normalized output contract requested for the run
    # ({schema, enforcement, schema_digest}); None when absent.
    output_contract: dict[str, Any] | None = None
    # Extracted JSON value from the agent message (any JSON type; None when
    # the message carried no parseable JSON or no contract was attached).
    structured_output: Any = None
    # Terminal verdict {enforcement, schema_digest, status, extraction,
    # violations}; None until a contracted run reaches a terminal state.
    contract_result: dict[str, Any] | None = None

    @property
    def id(self) -> str:
        return f"run-{self.n}"

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES


def record_to_dict(record: RunRecord) -> dict[str, Any]:
    return asdict(record)


def record_from_dict(data: Any) -> RunRecord:
    """Strict-ish decode; raises ``ValueError`` on unusable payloads."""
    if not isinstance(data, dict):
        raise ValueError("run record is not a dict")
    agent_id = data.get("agent_id")
    n = data.get("n")
    status = data.get("status")
    if not isinstance(agent_id, str) or not agent_id:
        raise ValueError("run record missing agent_id")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError("run record missing valid n")
    if status not in RUN_STATUSES:
        raise ValueError(f"run record has unknown status {status!r}")
    record = RunRecord(
        agent_id=agent_id,
        n=n,
        status=str(status),
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
    )
    for key in (
        "started_at",
        "finished_at",
        "result_text",
        "provider",
        "account_id",
        "model",
        "reasoning_effort",
    ):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"run record field {key} must be a string")
        setattr(record, key, value)
    error = data.get("error")
    if error is not None and not isinstance(error, dict):
        raise ValueError("run record field error must be a dict")
    record.error = dict(error) if error is not None else None
    usage = data.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise ValueError("run record field usage must be a dict")
        record.usage = {str(k): int(v) for k, v in usage.items()}
    refs = data.get("artifact_refs")
    if refs is not None:
        if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs):
            raise ValueError("run record field artifact_refs must be a string list")
        record.artifact_refs = list(refs)
    for key in ("output_contract", "contract_result"):
        value = data.get(key)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"run record field {key} must be a dict")
        setattr(record, key, dict(value) if value is not None else None)
    if "structured_output" in data:
        # Any JSON value is legal (including null); only presence is checked.
        record.structured_output = data.get("structured_output")
    return record


def corrupt_record(agent_id: str, n: int, detail: str) -> RunRecord:
    """Synthesized ``UNKNOWN`` record for an undecodable stored payload."""
    return RunRecord(
        agent_id=agent_id,
        n=n,
        status=UNKNOWN_RUN_STATUS,
        created_at="",
        updated_at="",
        error=run_error(
            "runtime_error",
            f"stored run record is corrupt: {detail}",
            source="runtime",
            retryable=True,
        ),
    )


def _decode(raw: Any, agent_id: str, n: int) -> RunRecord:
    try:
        return record_from_dict(raw)
    except (ValueError, TypeError) as exc:
        return corrupt_record(agent_id, n, str(exc))


@runtime_checkable
class RunStore(Protocol):
    """Persistence for ``RunRecord``s, keyed by ``(agent_id, n)``."""

    def get(self, agent_id: str, n: int) -> RunRecord | None:
        """Return the record, or None when absent. Corrupt payloads decode to
        a synthesized UNKNOWN record."""

    def put(self, record: RunRecord) -> None:
        """Insert or replace a record."""

    def list(self, agent_id: str) -> list[RunRecord]:
        """All records for one agent, sorted by n."""

    def delete(self, agent_id: str, n: int) -> None:
        """Remove a record (rollback of never-started runs)."""


class InMemoryRunStore:
    """Thread-safe dict store for tests and ephemeral deployments."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(agent_id: str, n: int) -> str:
        return f"{agent_id}/{n}"

    def get(self, agent_id: str, n: int) -> RunRecord | None:
        with self._lock:
            raw = self._items.get(self._key(agent_id, n))
        if raw is None:
            return None
        return _decode(raw, agent_id, n)

    def put(self, record: RunRecord) -> None:
        with self._lock:
            self._items[self._key(record.agent_id, record.n)] = record_to_dict(record)

    def list(self, agent_id: str) -> list[RunRecord]:
        prefix = f"{agent_id}/"
        with self._lock:
            items = [(k, v) for k, v in self._items.items() if k.startswith(prefix)]
        out = []
        for key, raw in items:
            try:
                n = int(key.rsplit("/", 1)[1])
            except (ValueError, IndexError):
                continue
            out.append(_decode(raw, agent_id, n))
        return sorted(out, key=lambda r: r.n)

    def delete(self, agent_id: str, n: int) -> None:
        with self._lock:
            self._items.pop(self._key(agent_id, n), None)


class FileRunStore:
    """Local durable store: one JSON file per run under ``root/<agent_id>/``.

    Writes are atomic (tmp file + rename) so a crash mid-write cannot leave a
    half-written record. The directory is created lazily on first write.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, agent_id: str, n: int) -> Path:
        return self._root / agent_id / f"run-{n}.json"

    def get(self, agent_id: str, n: int) -> RunRecord | None:
        path = self._path(agent_id, n)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, RecursionError) as exc:
            return corrupt_record(agent_id, n, f"invalid json: {exc}")
        return _decode(raw, agent_id, n)

    def put(self, record: RunRecord) -> None:
        path = self._path(record.agent_id, record.n)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(record_to_dict(record), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)

    def list(self, agent_id: str) -> list[RunRecord]:
        directory = self._root / agent_id
        try:
            entries = sorted(directory.glob("run-*.json"))
        except OSError:
            return []
        out = []
        for path in entries:
            try:
                n = int(path.stem.rsplit("-", 1)[1])
            except (ValueError, IndexError):
                continue
            record = self.get(agent_id, n)
            if record is not None:
                out.append(record)
        return sorted(out, key=lambda r: r.n)

    def delete(self, agent_id: str, n: int) -> None:
        with self._lock:
            self._path(agent_id, n).unlink(missing_ok=True)


_DICT_FANOUT = 8


class ModalDictRunStore:
    """Production store backed by ``modal.Dict``. Lazy-imports modal.

    Per-agent run index (SOR-199): a companion ``<name>-index`` Dict holds
    ``agent/<sha256(agent_id)>`` -> sorted run numbers and ``built`` once
    the index covers the whole store. ``modal.Dict`` enumeration is
    server-paged at ~one round-trip per key, so ``list(agent_id)`` reads
    one index doc plus the agent's own records through a bounded pool —
    never ``items()`` over the global store, which made
    ``GET /v1/agents/{id}/runs`` cost ~2s at a few hundred run keys and
    grows linearly with global run history. ``put`` writes the index
    *before* the record and ``delete`` removes the record *before* the
    index row, so a crash between the pair leaves at most a stale run
    number — skipped on read — never an invisible live run. A lost index
    update is repaired by ``rebuild_index`` (also the lazy migration for
    pre-index Dicts).
    """

    _IDX_PREFIX = "agent/"
    _IDX_BUILT = "built"

    def __init__(self, name: str = RUNS_DICT_NAME) -> None:
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

    @staticmethod
    def _key(agent_id: str, n: int) -> str:
        return f"{agent_id}/{n}"

    @staticmethod
    def _idx_key(agent_id: str) -> str:
        digest = hashlib.sha256(agent_id.encode("utf-8")).hexdigest()
        return f"{ModalDictRunStore._IDX_PREFIX}{digest}"

    def get(self, agent_id: str, n: int) -> RunRecord | None:
        key = self._key(agent_id, n)
        with observe("modal_dict.get", store=self._name, key=key):
            raw = self._d().get(key)
        if raw is None:
            return None
        return _decode(raw, agent_id, n)

    def put(self, record: RunRecord) -> None:
        self._index_add(record.agent_id, record.n)
        key = self._key(record.agent_id, record.n)
        with observe("modal_dict.put", store=self._name, key=key):
            self._d().put(key, record_to_dict(record))

    def list(self, agent_id: str) -> list[RunRecord]:
        raws: list[Any] | None = None
        ns: list[int] = []
        try:
            self._ensure_index()
            raw_ns = self._idx().get(self._idx_key(agent_id)) or []
            ns = sorted({int(v) for v in raw_ns if isinstance(v, int)})
            with observe("modal_dict.get_runs", store=self._name, agent_id=agent_id, runs=len(ns)):
                with ThreadPoolExecutor(max_workers=_DICT_FANOUT) as pool:
                    raws = list(pool.map(lambda n: self._d().get(self._key(agent_id, n)), ns))
        except Exception:
            raws = None
        if raws is not None:
            # Stale run numbers (record deleted between index write and
            # read) fetch None and are skipped.
            out = [_decode(raw, agent_id, n) for n, raw in zip(ns, raws) if isinstance(raw, dict)]
            return sorted(out, key=lambda r: r.n)
        # Index unavailable: fall back to the honest full enumeration
        # rather than fail the listing.
        prefix = f"{agent_id}/"
        out = []
        with observe("modal_dict.items", store=self._name, agent_id=agent_id):
            items: Iterator[tuple[Any, Any]] = self._d().items()
            for key, raw in items:
                if not isinstance(key, str) or not key.startswith(prefix):
                    continue
                try:
                    n = int(key.rsplit("/", 1)[1])
                except (ValueError, IndexError):
                    continue
                out.append(_decode(raw, agent_id, n))
        return sorted(out, key=lambda r: r.n)

    def delete(self, agent_id: str, n: int) -> None:
        try:
            with observe("modal_dict.pop", store=self._name, key=self._key(agent_id, n)):
                self._d().pop(self._key(agent_id, n))
        except KeyError:
            pass
        self._index_remove(agent_id, n)

    # --------------------------------------------------------- run index

    def _index_add(self, agent_id: str, n: int) -> None:
        try:
            with self._lock:
                key = self._idx_key(agent_id)
                ns = self._idx().get(key) or []
                if n in ns:
                    return
                self._idx().put(key, sorted([*ns, n]))
        except Exception:
            self._index_broken()

    def _index_remove(self, agent_id: str, n: int) -> None:
        try:
            with self._lock:
                key = self._idx_key(agent_id)
                ns = self._idx().get(key) or []
                if n not in ns:
                    return
                kept = [v for v in ns if v != n]
                if kept:
                    self._idx().put(key, kept)
                else:
                    self._idx().pop(key)
        except Exception:
            self._index_broken()

    def _index_broken(self) -> None:
        """Index maintenance failed: drop the marker so the next listing
        rebuilds and converges instead of serving a drifted index."""
        self._index_ready = False
        try:
            self._idx().pop(self._IDX_BUILT)
        except Exception:
            pass

    def _ensure_index(self) -> None:
        """Lazily build the index on the first listing — the safe
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
        """Re-enumerate keys once and rewrite every agent index doc.

        Idempotent and safe on a live store: converges the index (drops
        docs for agents with no runs left). Returns the number of run
        numbers indexed.
        """
        with observe("modal_dict.keys", store=self._name):
            keys = [k for k in self._d().keys() if isinstance(k, str)]
        groups: dict[str, set[int]] = {}
        for key in keys:
            head, sep, tail = key.rpartition("/")
            if not sep or not head or not tail.isdigit():
                continue
            groups.setdefault(head, set()).add(int(tail))
        with self._lock:
            keep = {self._idx_key(agent) for agent in groups}
            existing = [
                k
                for k in self._idx().keys()
                if isinstance(k, str) and k.startswith(self._IDX_PREFIX)
            ]
            for key in existing:
                if key not in keep:
                    self._idx().pop(key)
            for agent, ns in groups.items():
                self._idx().put(self._idx_key(agent), sorted(ns))
            self._idx().put(self._IDX_BUILT, b"1")
        self._index_ready = True
        return sum(len(ns) for ns in groups.values())


class RunLedger:
    """Transition rules over a ``RunStore``; the authoritative run API.

    * ``begin`` is idempotent — an existing record (open or terminal) is
      returned unchanged, so a retried ``post_message`` never resurrects a
      finished run.
    * ``finish`` / ``cancel`` write terminal records. Once a record is
      terminal it is returned unchanged: a ``turns/<n>.json`` that appears
      after a cancel, or a late read after teardown, cannot rewrite history.
    * ``discard`` removes only open records (turn rollback); terminal
      records are never deleted.
    """

    def __init__(
        self,
        store: RunStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()

    @property
    def store(self) -> RunStore:
        return self._store

    def _now(self) -> str:
        return self._clock().isoformat()

    def get(self, agent_id: str, n: int) -> RunRecord | None:
        return self._store.get(agent_id, n)

    def list(self, agent_id: str) -> list[RunRecord]:
        return self._store.list(agent_id)

    def begin(
        self,
        *,
        agent_id: str,
        n: int,
        provider: str | None = None,
        account_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        status: str = "RUNNING",
        artifact_refs: list[str] | None = None,
        output_contract: dict[str, Any] | None = None,
    ) -> RunRecord:
        if status not in OPEN_RUN_STATUSES:
            raise ValueError(f"begin status must be open, got {status!r}")
        with self._lock:
            existing = self._store.get(agent_id, n)
            if existing is not None:
                return existing
            now = self._now()
            record = RunRecord(
                agent_id=agent_id,
                n=n,
                status=status,
                created_at=now,
                updated_at=now,
                started_at=now if status == "RUNNING" else None,
                provider=provider,
                account_id=account_id,
                model=model,
                reasoning_effort=reasoning_effort,
                artifact_refs=list(artifact_refs)
                if artifact_refs is not None
                else default_artifact_refs(n),
                output_contract=dict(output_contract) if output_contract is not None else None,
            )
            self._store.put(record)
            return record

    def mark_running(self, agent_id: str, n: int) -> RunRecord | None:
        """``CREATING → RUNNING`` (async create lands with A2); terminal safe."""
        with self._lock:
            existing = self._store.get(agent_id, n)
            if existing is None or existing.terminal or existing.status == "RUNNING":
                return existing
            now = self._now()
            record = replace(
                existing, status="RUNNING", started_at=existing.started_at or now, updated_at=now
            )
            self._store.put(record)
            return record

    def finish(
        self,
        agent_id: str,
        n: int,
        *,
        status: str,
        result_text: str | None = None,
        error: dict[str, Any] | None = None,
        usage: dict[str, int] | None = None,
        artifact_refs: list[str] | None = None,
        provider: str | None = None,
        account_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        created_at: str | None = None,
        started_at: str | None = None,
        structured_output: Any = None,
        contract_result: dict[str, Any] | None = None,
    ) -> RunRecord:
        """Persist a terminal outcome; a terminal record is never rewritten."""
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"finish status must be terminal, got {status!r}")
        with self._lock:
            existing = self._store.get(agent_id, n)
            if existing is not None and existing.terminal:
                return existing
            now = self._now()
            record = existing or RunRecord(
                agent_id=agent_id,
                n=n,
                status=status,
                created_at=created_at or now,
                updated_at=now,
                artifact_refs=default_artifact_refs(n),
            )
            record.status = status
            record.updated_at = now
            record.finished_at = now
            if record.started_at is None:
                record.started_at = started_at
            if result_text is not None:
                record.result_text = result_text
            if error is not None:
                record.error = error
            if usage is not None:
                record.usage = dict(usage)
            if contract_result is not None:
                # SOR-130: contract_result is the terminal verdict marker —
                # when present, structured_output is persisted verbatim
                # (a valid contract may legitimately produce JSON null).
                record.contract_result = dict(contract_result)
                record.structured_output = structured_output
            if artifact_refs:
                record.artifact_refs = list(artifact_refs)
            for key, value in (
                ("provider", provider),
                ("account_id", account_id),
                ("model", model),
                ("reasoning_effort", reasoning_effort),
            ):
                if value is not None:
                    setattr(record, key, value)
            self._store.put(record)
            return record

    def cancel(self, agent_id: str, n: int, *, message: str = "run cancelled") -> RunRecord:
        return self.finish(
            agent_id,
            n,
            status="CANCELLED",
            error=run_error("cancelled", message, source="control"),
        )

    def attach_artifacts(self, agent_id: str, n: int, refs: list[str]) -> RunRecord | None:
        """Append artifact references to a record without touching status.

        ``artifact_refs`` are evidence pointers, not outcome — appending a
        durable ``artifact://<id>`` ref to a terminal record is legal (the
        snapshot may land only just before teardown) and never rewrites the
        monotonic status history. Returns the record, or None when absent.
        """
        with self._lock:
            existing = self._store.get(agent_id, n)
            if existing is None:
                return None
            merged = list(existing.artifact_refs)
            for ref in refs:
                if ref not in merged:
                    merged.append(ref)
            if merged == existing.artifact_refs:
                return existing
            record = replace(existing, artifact_refs=merged)
            self._store.put(record)
            return record

    def discard(self, agent_id: str, n: int) -> None:
        """Drop an open record (turn rolled back before exec). Never removes
        a terminal record."""
        with self._lock:
            existing = self._store.get(agent_id, n)
            if existing is not None and existing.terminal:
                return
            self._store.delete(agent_id, n)


__all__ = [
    "OPEN_RUN_STATUSES",
    "RUN_STATUSES",
    "TERMINAL_RUN_STATUSES",
    "UNKNOWN_RUN_STATUS",
    "FileRunStore",
    "InMemoryRunStore",
    "ModalDictRunStore",
    "RunLedger",
    "RunRecord",
    "RunStore",
    "corrupt_record",
    "default_artifact_refs",
    "outcome_from_turn_payload",
    "record_from_dict",
    "record_to_dict",
    "run_error",
]
