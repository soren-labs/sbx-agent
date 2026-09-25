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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from control.config import TERMINAL_STATUSES
from control.run_errors import RunError, run_error_for_run
from control.run_store import TERMINAL_RUN_STATUSES, RunLedger
from control.service import SessionConflict, release_lease
from control.workspace import WORKSPACE_INVALID, WorkspaceError, WorkspaceSpec

RUN_TERMINAL = TERMINAL_RUN_STATUSES

# Monotonic run progression; terminal states are sticky and never rewritten.
_RUN_ORDER = {
    "CREATING": 0,
    "QUEUED": 0,
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

    ``error`` carries the canonical SOR-82 structured diagnostic
    (``{code, source, message, retryable, retry_after?}``).
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

    ``LedgerRunStates`` binds this to the durable run ledger; the in-memory
    fallback below keeps ledger-less tests working.
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

    ``agent_id``/``body`` are populated when the handler's response is known
    (``complete``); ``done`` is set only when the create has fully resolved —
    the background worker's sandbox allocation settled — so a duplicate that
    lands mid-provision waits for the worker instead of racing it (SOR-82 A4
    in-flight retry acceptance). On failure the entry is abandoned so a retry
    may proceed (only successful creates pin the key).
    """

    fingerprint: str
    done: threading.Event = field(default_factory=threading.Event)
    agent_id: str | None = None
    body: dict[str, Any] | None = None


class IdempotencyStore:
    """``(api_key_id, Idempotency-Key)`` → one logical agent create.

    In-memory per control-plane process; the durable bound lives on the
    session record (``idempotency_key``/``idempotency_fingerprint``) so a
    retry that lands after a restart still dedups. Concurrent duplicates wait
    for the in-flight create — including its worker allocation — and replay
    its result, so a lost response or retry never spawns a second sandbox.
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
                if entry.done.is_set() and entry.agent_id is not None:
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
        """Record the handler's response; the create resolves on ``settle``."""
        with self._lock:
            if self._entries.get((key_id, idem_key)) is entry:
                entry.agent_id = agent_id
                entry.body = body

    def settle(self, key_id: str, idem_key: str, entry: _IdemEntry) -> None:
        """Mark the create fully resolved (worker allocation settled)."""
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
    on_provisioned: Callable[[], None] | None = None,
    workspace: dict[str, Any] | None = None,
    handoff: dict[str, Any] | None = None,
    git: dict[str, Any] | None = None,
    resources: dict[str, Any] | None = None,
) -> None:
    """Spawn the background create → init → first-turn worker (SOR-82 A2).

    ``on_provisioned`` fires once the worker's sandbox allocation has
    resolved (created, failed, or the session was closed underneath) — an
    idempotent duplicate waits on it so a retry never races the worker.

    ``workspace``/``handoff`` carry the SOR-83 declarations
    (``{repo, base_ref, base_sha}`` / ``{artifact_id|head_sha|pull_request}``):
    after the sandbox is provisioned the worker prepares the declared
    checkout (and applies the handoff) before run-1 may dispatch. ``git``
    carries the SOR-128 collaboration policy the prepare persists.
    ``resources`` carries the resolved SOR-129 session-resource refs
    (``{"secrets": [names], "mcp": [entries]}``) — injected into this
    sandbox only.
    """
    thread = threading.Thread(
        target=_first_run_worker,
        args=(
            plane,
            v1,
            run_states,
            session_id,
            provider,
            account_id,
            secret_name,
            on_provisioned,
            workspace,
            handoff,
            git,
            resources,
        ),
        daemon=True,
        name=f"sbx-v1-create-{session_id[:8]}",
    )
    thread.start()


def _release_session_lease(v1: Any, session_id: str) -> None:
    try:
        release_lease(v1, session_id)
    except Exception:
        pass


def _persist_run1_terminal(
    run_states: RunStateStore, session_id: str, status: str, error: dict[str, Any]
) -> None:
    """Best-effort terminal persist for run-1.

    Never raises: the caller still owes the scheduler-lease release, and a
    ledger/store failure must not wedge the worker before it runs.
    """
    try:
        run_states.transition(session_id, 1, status, error=error)
    except Exception:
        pass


def _workspace_error(exc: BaseException) -> dict[str, Any]:
    """Canonical structured error for a workspace/handoff startup failure.

    The SOR-83 machine code (``base_sha_mismatch`` et al.) rides inside the
    message so the durable run record stays explicit while ``code`` keeps the
    frozen run-error taxonomy. Deterministic declaration failures are not
    retryable: replaying the same request would fail identically.
    """
    message = str(exc)[:300] or exc.__class__.__name__
    return RunError("runtime_error", "control", message, retryable=False).public()


def _prepare_workspace(
    plane: Any,
    session_id: str,
    workspace: dict[str, Any],
    handoff: dict[str, Any] | None,
    git: dict[str, Any] | None = None,
    env_restored: bool = False,
) -> None:
    """Prepare the declared workspace on the fresh sandbox (SOR-83).

    Runs after ``provision_session`` bound the sandbox and before run-1
    dispatches. A handoff (``artifact_id``, exact ``head_sha`` or a pinned
    ``pull_request`` ref) replaces the plain clone: it still validates the
    declared base, then applies the referenced artifact or checks out the
    referenced commit. ``git`` (SOR-128) is the collaboration policy the
    prepare persists on the workspace record.

    SOR-127: ``env_restored`` means the sandbox was provisioned from a
    prepared-environment snapshot — the clone is skipped and
    ``prepare_restored`` verifies the restored HEAD against the declared
    ``base_sha`` (fail closed) before any handoff applies on top.
    """
    workspaces = getattr(plane, "workspaces", None)
    if workspaces is None:
        raise WorkspaceError(WORKSPACE_INVALID, "workspace service is not configured")
    rec = plane.get(session_id)
    handle = rec.handle() if rec is not None else None
    if handle is None:
        raise WorkspaceError(
            WORKSPACE_INVALID, f"agent {session_id} has no live sandbox for workspace prepare"
        )
    spec = WorkspaceSpec(
        repo=str(workspace.get("repo") or ""),
        base_ref=str(workspace.get("base_ref") or ""),
        base_sha=str(workspace.get("base_sha") or ""),
    )
    if env_restored:
        workspaces.prepare_restored(handle, session_id, spec, git=git)
    handoff = handoff or {}
    artifact_id = handoff.get("artifact_id")
    head_sha = handoff.get("head_sha")
    pull_request = handoff.get("pull_request")
    if artifact_id or head_sha or pull_request:
        handoffs = getattr(plane, "handoffs", None)
        if handoffs is None:
            raise WorkspaceError(WORKSPACE_INVALID, "handoff service is not configured")
        if artifact_id:
            handoffs.prepare_from_artifact(handle, session_id, str(artifact_id), spec=spec, git=git)
        elif pull_request:
            handoffs.prepare_from_pull_request(
                handle,
                session_id,
                str(pull_request.get("ref") or ""),
                str(pull_request.get("head_sha") or ""),
                spec=spec,
                git=git,
            )
        else:
            handoffs.prepare_from_head(handle, session_id, str(head_sha), spec=spec, git=git)
    elif not env_restored:
        # A restored environment already has the workspace prepared via
        # ``prepare_restored`` above — a fresh clone would collide with the
        # snapshotted checkout.
        workspaces.prepare(handle, session_id, spec, git=git)


def _drain_queued_best_effort(plane: Any, session_id: str) -> None:
    """SOR-224: every run-1 terminal path in the worker lets the durable
    queue head dispatch — a failed run-1 must not strand queued follow-ups."""
    drain = getattr(plane, "drain_queued", None)
    if callable(drain):
        try:
            drain(session_id)
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
    on_provisioned: Callable[[], None] | None = None,
    workspace: dict[str, Any] | None = None,
    handoff: dict[str, Any] | None = None,
    git: dict[str, Any] | None = None,
    resources: dict[str, Any] | None = None,
) -> None:
    """Advance run-1 CREATING → RUNNING → (terminal) around sandbox startup.

    Provision failure persists run-1 as ``ERROR`` and frees the scheduler
    lease; a session that went terminal underneath (delete, reaper) yields
    ``CANCELLED``. A cancel that lands before dispatch skips the turn. A
    declared workspace (SOR-83) is prepared before the turn dispatches —
    failure is an explicit run-1 ``ERROR`` and the agent is closed rather
    than left to run on the wrong version.
    """
    # SOR-127: when the environment cache is wired and a workspace is
    # declared, resolve the last-known-good build record first — a usable
    # snapshot restores the sandbox instead of a cold create, and the
    # prepare step below still verifies the exact ``base_sha``.
    envs = getattr(plane, "environments", None)
    env_spec = None
    env_record = None
    if workspace is not None and envs is not None:
        try:
            env_spec = envs.spec_for(workspace, provider=provider)
            env_record = envs.resolve(env_spec)
        except Exception:
            env_spec = None
            env_record = None
    env_snapshot = env_record.snapshot_ref if env_record is not None else None
    try:
        plane.provision_session(
            session_id,
            provider=provider,
            account_id=account_id,
            secret_name=secret_name,
            resource_secrets=(resources or {}).get("secrets"),
            mcp_servers=(resources or {}).get("mcp"),
            env_snapshot=env_snapshot,
        )
    except KeyError:
        _persist_run1_terminal(run_states, session_id, "CANCELLED", _closed_error())
        _release_session_lease(v1, session_id)
        _drain_queued_best_effort(plane, session_id)
        return
    except SessionConflict as exc:
        rec = plane.get(session_id)
        if rec is not None and rec.status == "closed":
            _persist_run1_terminal(run_states, session_id, "CANCELLED", _closed_error())
        else:
            _persist_run1_terminal(run_states, session_id, "ERROR", _runtime_error(exc))
        _release_session_lease(v1, session_id)
        _drain_queued_best_effort(plane, session_id)
        return
    except Exception as exc:
        _persist_run1_terminal(run_states, session_id, "ERROR", _runtime_error(exc))
        # SOR-127: a provision failure on a snapshot restore means the
        # record's snapshot may be dead (expired/corrupt) — invalidate it so
        # subsequent runs rebuild instead of wedging on the same ref.
        if env_snapshot is not None and envs is not None and env_spec is not None:
            try:
                envs.invalidate(env_spec.key)
            except Exception:
                pass
        _release_session_lease(v1, session_id)
        _drain_queued_best_effort(plane, session_id)
        return
    finally:
        if on_provisioned is not None:
            try:
                on_provisioned()
            except Exception:
                pass

    # SOR-83: a declared workspace is cloned / handoff-applied before run-1
    # may dispatch. The terminal check runs first so a cancelled run-1 never
    # pays for a clone; a prepare failure is an explicit run-1 ERROR and the
    # agent is closed rather than left to run on the wrong version.
    if workspace is not None:
        try:
            state = run_states.get(session_id, 1)
            if state is not None and state.status in RUN_TERMINAL:
                plane.discard_queued_first_turn(session_id)
                _drain_queued_best_effort(plane, session_id)
                return
            _prepare_workspace(
                plane,
                session_id,
                workspace,
                handoff,
                git,
                env_restored=env_snapshot is not None,
            )
        except Exception as exc:
            _persist_run1_terminal(run_states, session_id, "ERROR", _workspace_error(exc))
            # SOR-127: a restored environment that fails the base_sha gate
            # is a poisoned snapshot — drop the record so the next run
            # rebuilds rather than re-serving it.
            if env_snapshot is not None and envs is not None and env_spec is not None:
                try:
                    envs.invalidate(env_spec.key)
                except Exception:
                    pass
            try:
                plane.close(session_id)
            except Exception:
                pass
            _release_session_lease(v1, session_id)
            _drain_queued_best_effort(plane, session_id)
            return
        # SOR-127: cache fill — a successful cold prepare means this
        # workspace's environment can be snapshotted for reuse. Best-effort:
        # the record carries the outcome; a failed build never fails the run.
        if envs is not None and env_spec is not None and env_record is None:
            try:
                envs.try_build(env_spec)
            except Exception:
                pass

    # Provisioned → idle. A cancel/delete may have landed during cold start;
    # the run-state read itself is guarded so a store hiccup cannot kill the
    # worker with run-1 still open and the queued turn still reserved.
    try:
        state = run_states.get(session_id, 1)
        if state is not None and state.status in RUN_TERMINAL:
            plane.discard_queued_first_turn(session_id)
            _drain_queued_best_effort(plane, session_id)
            return
        plane.post_queued_first_turn(session_id)
    except Exception as exc:
        rec = plane.get(session_id)
        live = rec is not None and rec.status not in TERMINAL_STATUSES
        if live:
            _persist_run1_terminal(run_states, session_id, "ERROR", _runtime_error(exc))
        else:
            _persist_run1_terminal(run_states, session_id, "CANCELLED", _closed_error())
            _release_session_lease(v1, session_id)
        _drain_queued_best_effort(plane, session_id)
        return
    dispatched = run_states.transition(session_id, 1, "RUNNING")
    if dispatched is not None and dispatched.status != "RUNNING":
        # Cancelled between the check above and the actual dispatch — stop
        # the turn that just started so no billed work continues.
        try:
            plane.stop(session_id)
        except Exception:
            pass
    _drain_queued_best_effort(plane, session_id)
