"""Async create lifecycle + idempotent-create bookkeeping (SOR-82 A2).

``POST /v1/agents`` returns right after the session record and a ``CREATING``
run exist; a daemon thread then provisions the sandbox, runs ``runner init``
and dispatches the queued first turn. Startup failures persist run-1 as
``ERROR`` — never silently dropped.

Run-state seam: ``RunStateStore`` is the minimal surface A2 needs (explicit
``CREATING`` before a turn exists, persisted terminal states). With SOR-82
integrated, ``LedgerRunStates`` binds the seam to the durable run ledger
(``control.run_store``) — the ledger is the source of truth, so CREATING /
RUNNING / terminal transitions survive restarts. ``InMemoryRunStates``
remains the fallback when no ledger is attached.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from control.config import TERMINAL_STATUSES
from control.run_errors import RunError, run_error_for_run
from control.run_store import TERMINAL_RUN_STATUSES, RunLedger
from control.service import SessionConflict, release_lease

RUN_TERMINAL = TERMINAL_RUN_STATUSES

# Monotonic run progression; terminal states are sticky and never rewritten.
_RUN_ORDER = {
    "CREATING": 0,
    "RUNNING": 1,
    "FINISHED": 2,
    "ERROR": 2,
    "CANCELLED": 2,
    "EXPIRED": 2,
}

_IDEM_WAIT_S = 60.0


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunState:
    """Explicit run lifecycle record — the v1 view of one run's state.

    ``error`` carries a minimal ``{code, message}`` diagnostic; A3 (SOR-86)
    owns the full structured-error taxonomy.
    """

    n: int
    status: str
    created_at: str
    updated_at: str
    prompt: str | None = None
    error: dict[str, Any] | None = None


@runtime_checkable
class RunStateStore(Protocol):
    """Minimal run-state surface consumed by the ``/v1`` routes.

    A1 (SOR-88) implements this against the durable run ledger; the in-memory
    fallback below keeps local mode and tests working.
    """

    def begin(
        self, agent_id: str, n: int, *, prompt: str | None = None, status: str = "CREATING"
    ) -> RunState:
        """Create the run record (idempotent per ``(agent_id, n)``)."""

    def get(self, agent_id: str, n: int) -> RunState | None:
        """Return the record or None."""

    def list(self, agent_id: str) -> list[RunState]:
        """All known runs for an agent."""

    def transition(
        self, agent_id: str, n: int, status: str, *, error: dict[str, Any] | None = None
    ) -> RunState | None:
        """Monotonic status transition; terminal states are sticky.

        Returns the (possibly unchanged) record, or None when unknown.
        """


class InMemoryRunStates:
    """Thread-safe dict-backed ``RunStateStore`` fallback."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[int, RunState]] = {}
        self._lock = threading.Lock()

    def begin(
        self, agent_id: str, n: int, *, prompt: str | None = None, status: str = "CREATING"
    ) -> RunState:
        with self._lock:
            runs = self._runs.setdefault(agent_id, {})
            existing = runs.get(n)
            if existing is not None:
                return existing
            now = _iso_now()
            record = RunState(n=n, status=status, created_at=now, updated_at=now, prompt=prompt)
            runs[n] = record
            return record

    def get(self, agent_id: str, n: int) -> RunState | None:
        with self._lock:
            return self._runs.get(agent_id, {}).get(n)

    def list(self, agent_id: str) -> list[RunState]:
        with self._lock:
            return sorted(self._runs.get(agent_id, {}).values(), key=lambda r: r.n)

    def transition(
        self, agent_id: str, n: int, status: str, *, error: dict[str, Any] | None = None
    ) -> RunState | None:
        with self._lock:
            record = self._runs.get(agent_id, {}).get(n)
            if record is None or record.status in RUN_TERMINAL:
                return record
            if _RUN_ORDER.get(status, -1) < _RUN_ORDER.get(record.status, -1):
                return record
            record.status = status
            record.updated_at = _iso_now()
            if error is not None:
                record.error = error
            return record


class LedgerRunStates:
    """``RunStateStore`` over the durable ``RunLedger`` (SOR-82 integration).

    The ledger is the source of truth: CREATING/RUNNING records and terminal
    transitions are persisted, so worker state survives control-plane
    restarts and sandbox teardown. ``prompt`` is accepted for protocol
    compatibility; the durable record tracks artifacts, not prompt text.
    """

    def __init__(self, ledger: RunLedger) -> None:
        self._ledger = ledger

    def begin(
        self, agent_id: str, n: int, *, prompt: str | None = None, status: str = "CREATING"
    ) -> Any:
        return self._ledger.begin(agent_id=agent_id, n=n, status=status)

    def get(self, agent_id: str, n: int) -> Any:
        return self._ledger.get(agent_id, n)

    def list(self, agent_id: str) -> list[Any]:
        return self._ledger.list(agent_id)

    def transition(
        self, agent_id: str, n: int, status: str, *, error: dict[str, Any] | None = None
    ) -> Any:
        if status == "RUNNING":
            record = self._ledger.mark_running(agent_id, n)
            if record is None:
                record = self._ledger.begin(agent_id=agent_id, n=n, status="RUNNING")
            return record
        if status in RUN_TERMINAL:
            if error is None:
                # Terminal records must carry a structured error: a CANCELLED
                # or ERROR run with ``error=None`` is undiagnosable.
                defaulted = run_error_for_run(status, cancelled=True)
                error = defaulted.public() if defaulted is not None else None
            return self._ledger.finish(agent_id, n, status=status, error=error)
        if status == "CREATING":
            return self._ledger.begin(agent_id=agent_id, n=n, status="CREATING")
        return self._ledger.get(agent_id, n)


@dataclass
class _IdemEntry:
    """One claimed ``Idempotency-Key``: in-flight until ``done`` is set.

    ``agent_id``/``body`` are populated on success; on failure the entry is
    abandoned so a retry may proceed (only successful creates pin the key).
    """

    fingerprint: str
    done: threading.Event = field(default_factory=threading.Event)
    agent_id: str | None = None
    body: dict[str, Any] | None = None


class IdempotencyStore:
    """``(api_key_id, Idempotency-Key)`` → one logical agent create.

    In-memory per control-plane process — the durable bound is A1/P2-C scope.
    Concurrent duplicates wait for the in-flight create and replay its result,
    so a lost response or retry never spawns a second sandbox.
    """

    def __init__(self, wait_s: float = _IDEM_WAIT_S) -> None:
        self._entries: dict[tuple[str, str], _IdemEntry] = {}
        self._lock = threading.Lock()
        self._wait_s = wait_s

    def claim(self, key_id: str, idem_key: str, fingerprint: str) -> tuple[str, _IdemEntry | None]:
        """→ ``("owned"|"hit"|"conflict"|"timeout", entry)``.

        ``owned`` — caller performs the create, then ``complete``/``abandon``.
        ``hit`` — ``entry.agent_id``/``entry.body`` hold the earlier result.
        ``conflict`` — same key, different request body.
        ``timeout`` — an in-flight create with this key did not finish in time.
        """
        map_key = (key_id, idem_key)
        while True:
            with self._lock:
                entry = self._entries.get(map_key)
                if entry is None or (entry.done.is_set() and entry.agent_id is None):
                    entry = _IdemEntry(fingerprint=fingerprint)
                    self._entries[map_key] = entry
                    return "owned", entry
                if entry.fingerprint != fingerprint:
                    return "conflict", None
                if entry.agent_id is not None:
                    return "hit", entry
            if not entry.done.wait(timeout=self._wait_s):
                return "timeout", entry
            if entry.agent_id is not None:
                return "hit", entry
            # Abandoned by the failed owner — loop and try to own the key.

    def complete(
        self,
        key_id: str,
        idem_key: str,
        entry: _IdemEntry,
        *,
        agent_id: str,
        body: dict[str, Any],
    ) -> None:
        with self._lock:
            if self._entries.get((key_id, idem_key)) is entry:
                entry.agent_id = agent_id
                entry.body = body
        entry.done.set()

    def abandon(self, key_id: str, idem_key: str, entry: _IdemEntry) -> None:
        """Free the key after a failed create so a retry may proceed."""
        with self._lock:
            if self._entries.get((key_id, idem_key)) is entry:
                del self._entries[(key_id, idem_key)]
        entry.done.set()


def request_fingerprint(body: Any) -> str:
    """Stable digest of the create request body for key-reuse detection."""
    dump = getattr(body, "model_dump_json", None)
    raw = dump() if callable(dump) else repr(body)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _runtime_error(exc: BaseException) -> dict[str, Any]:
    """Canonical structured error for a worker-side startup failure."""
    message = str(exc)[:300] or exc.__class__.__name__
    return RunError("runtime_error", "runtime", message, retryable=True).public()


def _closed_error() -> dict[str, Any]:
    """Canonical error for a run cancelled because its agent was closed."""
    cancelled = run_error_for_run("CANCELLED", cancelled=False)
    return cancelled.public() if cancelled is not None else _runtime_error(RuntimeError())


def launch_first_run(
    *,
    plane: Any,
    v1: Any,
    run_states: RunStateStore,
    session_id: str,
    provider: str,
    account_id: str,
    secret_name: str | None,
) -> None:
    """Spawn the background create → init → first-turn worker (SOR-82 A2)."""
    thread = threading.Thread(
        target=_first_run_worker,
        args=(plane, v1, run_states, session_id, provider, account_id, secret_name),
        daemon=True,
        name=f"sbx-v1-create-{session_id[:8]}",
    )
    thread.start()


def _release_session_lease(v1: Any, session_id: str) -> None:
    try:
        release_lease(v1, session_id)
    except Exception:
        pass


def _first_run_worker(
    plane: Any,
    v1: Any,
    run_states: RunStateStore,
    session_id: str,
    provider: str,
    account_id: str,
    secret_name: str | None,
) -> None:
    """Advance run-1 CREATING → RUNNING → (terminal) around sandbox startup.

    Provision failure persists run-1 as ``ERROR`` and frees the scheduler
    lease; a session that went terminal underneath (delete, reaper) yields
    ``CANCELLED``. A cancel that lands before dispatch skips the turn.
    """
    try:
        plane.provision_session(
            session_id, provider=provider, account_id=account_id, secret_name=secret_name
        )
    except KeyError:
        run_states.transition(session_id, 1, "CANCELLED", error=_closed_error())
        _release_session_lease(v1, session_id)
        return
    except SessionConflict as exc:
        rec = plane.get(session_id)
        if rec is not None and rec.status == "closed":
            run_states.transition(session_id, 1, "CANCELLED", error=_closed_error())
        else:
            run_states.transition(session_id, 1, "ERROR", error=_runtime_error(exc))
        _release_session_lease(v1, session_id)
        return
    except Exception as exc:
        run_states.transition(session_id, 1, "ERROR", error=_runtime_error(exc))
        _release_session_lease(v1, session_id)
        return

    # Provisioned → idle. A cancel/delete may have landed during cold start.
    state = run_states.get(session_id, 1)
    if state is not None and state.status in RUN_TERMINAL:
        plane.discard_queued_first_turn(session_id)
        return
    try:
        plane.post_queued_first_turn(session_id)
    except Exception as exc:
        rec = plane.get(session_id)
        live = rec is not None and rec.status not in TERMINAL_STATUSES
        if live:
            run_states.transition(session_id, 1, "ERROR", error=_runtime_error(exc))
        else:
            run_states.transition(session_id, 1, "CANCELLED", error=_closed_error())
            _release_session_lease(v1, session_id)
        return
    dispatched = run_states.transition(session_id, 1, "RUNNING")
    if dispatched is not None and dispatched.status != "RUNNING":
        # Cancelled between the check above and the actual dispatch — stop
        # the turn that just started so no billed work continues.
        try:
            plane.stop(session_id)
        except Exception:
            pass
