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

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from control.config import RUNS_DICT_NAME
from control.run_errors import (
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


def outcome_from_turn_payload(
    payload: dict[str, Any] | None,
) -> tuple[str, dict[str, Any] | None, str | None, dict[str, int] | None]:
    """Map a ``turns/<n>.json`` payload to (run status, error, result, usage).

    A missing/unreadable payload is explicit ``ERROR`` +
    ``runtime_error`` — never inferred success.
    """
    if payload is None:
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
    usage = (
        {k: int(usage_raw[k]) for k in _USAGE_KEYS if k in usage_raw}
        if isinstance(usage_raw, dict)
        else None
    )
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
        err = run_error_from_turn(payload) or RunError(
            "cancelled", "control", "run cancelled"
        )
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
    artifact_refs: list[str] = field(default_factory=list)

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
    for key in ("started_at", "finished_at", "result_text", "provider", "account_id", "model"):
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
        except json.JSONDecodeError as exc:
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


class ModalDictRunStore:
    """Production store backed by ``modal.Dict``. Lazy-imports modal."""

    def __init__(self, name: str = RUNS_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    @staticmethod
    def _key(agent_id: str, n: int) -> str:
        return f"{agent_id}/{n}"

    def get(self, agent_id: str, n: int) -> RunRecord | None:
        raw = self._d().get(self._key(agent_id, n))
        if raw is None:
            return None
        return _decode(raw, agent_id, n)

    def put(self, record: RunRecord) -> None:
        self._d().put(self._key(record.agent_id, record.n), record_to_dict(record))

    def list(self, agent_id: str) -> list[RunRecord]:
        prefix = f"{agent_id}/"
        out = []
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
            self._d().pop(self._key(agent_id, n))
        except KeyError:
            return


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
        status: str = "RUNNING",
        artifact_refs: list[str] | None = None,
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
                artifact_refs=list(artifact_refs)
                if artifact_refs is not None
                else default_artifact_refs(n),
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
        created_at: str | None = None,
        started_at: str | None = None,
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
            if artifact_refs:
                record.artifact_refs = list(artifact_refs)
            for key, value in (
                ("provider", provider),
                ("account_id", account_id),
                ("model", model),
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
