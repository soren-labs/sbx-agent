"""Session-first ``/v2`` routes (SOR-256).

Nine endpoints, one resource: the Session. Every mutating route delegates
to the V1 task handlers — a session IS a task record (``sess_``-prefixed)
on the same engine — then answers with the sanitized Session projection.
Nothing here creates a second runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import Depends, Header, Query, Request

from control.api_v1 import revisions as _revisions_api
from control.api_v1 import routes as _routes
from control.api_v1 import tasks as _tasks
from control.api_v1.deps import (
    agents_key,
    get_artifact_store,
    get_capabilities,
    get_plane,
    get_registry,
    get_repo_resolver,
    get_resources,
    get_revisions,
    get_run_reporter,
    get_run_states,
    get_scheduler,
    get_task_store,
    get_v1_state,
    get_workflow_service,
)
from control.api_v1.error_catalog import spec_for
from control.api_v1.errors import V1ApiError, not_found
from control.api_v1.lifecycle import RUN_TERMINAL, RunStateStore, request_fingerprint
from control.api_v1.schemas import Prompt
from control.api_v1.state import V1State
from control.api_v2 import events_hub as _hub_mod
from control.api_v2 import router
from control.api_v2.diff import parse_unified_diff
from control.api_v2.projection import (
    is_session_id,
    latest_run_error,
    live_extras,
    map_session_status,
    new_session_id,
    revision_view,
    run_view,
    session_view,
)
from control.api_v2.schemas import (
    CreateSessionRequest,
    SessionChangesDiffResponse,
    SessionChangesResponse,
    SessionDeliverRequest,
    SessionDeliverResponse,
    SessionDetailResponse,
    SessionListResponse,
    SessionMessageRequest,
    SessionMessageResponse,
    SessionResponse,
    SessionRetryRequest,
    SessionRetryResponse,
)
from control.artifacts import ArtifactError
from control.config import TERMINAL_STATUSES, env_float, env_int
from control.ports import AccountRegistry, ApiKey, Scheduler
from control.sandbox_io import read_text, sandbox_env
from control.tasks import TaskRecord, TaskStore, _iso_now

RUNS_FIRST_VIEW_MAX = 25
"""A session detail inlines at most this many recent runs — bounded first
view, the SSE stream carries the full activity history."""

_STATUS_POLL_S = 0.5
"""SSE status-transition cadence — session.status frames emit on change,
not just on connect, so clients see queued→running→finished live."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _require_session(task_store: TaskStore, key: ApiKey, session_id: str) -> TaskRecord:
    """The caller's session, or a plain ``not_found`` (no cross-owner or
    cross-surface leaks: a ``task_…`` id is not a session either)."""
    record = task_store.get(session_id)
    if record is None or record.owner != key.id or not is_session_id(record.id):
        raise not_found("session not found")
    return record


def _require_session_for_cancel_ack(
    task_store: TaskStore, key: ApiKey, session_id: str
) -> TaskRecord:
    """Cheap-but-safe record for the optimistic cancel ACK path.

    Owner/id are immutable, so a locally cached *non-terminal* row can be
    used even after its normal read TTL: the durable cancel marker makes the
    request real before the response and ``cancel_task`` is idempotent if the
    task happened to finish concurrently. A cached terminal row is different:
    another control-plane instance may already have retried it back to
    running, so terminal/no-op decisions always re-read through the normal
    store path before returning.
    """
    peek = getattr(task_store, "peek_cached", None)
    cached = peek(session_id) if callable(peek) else None
    if cached is not None:
        if cached.owner != key.id or not is_session_id(cached.id):
            raise not_found("session not found")
        if cached.status not in _tasks._TASK_TERMINAL:
            return cached
    return _require_session(task_store, key, session_id)


def _prefetch_reads(record: TaskRecord, plane: Any) -> dict[str, Any]:
    """Concurrent remote reads the settle+view path needs: live session
    record, workspace doc, run-ledger rows.

    Serial these are three remote round-trips (Dict gets + list); fanned
    out they cost one round-trip wall-clock, which is what keeps a cold
    ``GET /v2/sessions/{id}`` inside its P95 budget at Modal latency.
    """
    agent_id = record.agent_id
    out: dict[str, Any] = {"ws": None, "rec": None, "rows": None}
    if agent_id is None:
        return out
    ledger = getattr(plane, "run_ledger", None)
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="sbx-read") as pool:
        f_rec = pool.submit(plane.get, agent_id)
        f_ws = pool.submit(_tasks._ws_record, plane, agent_id)
        f_rows = pool.submit(lambda: list(ledger.list(agent_id))) if ledger is not None else None
        try:
            out["rec"] = f_rec.result()
        except Exception:
            out["rec"] = None
        try:
            out["ws"] = f_ws.result()
        except Exception:
            out["ws"] = None
        if f_rows is not None:
            try:
                out["rows"] = f_rows.result()
            except Exception:
                out["rows"] = None
        if out["rows"] is not None:
            out["statuses"] = sorted((r.n, r.status) for r in out["rows"])
    return out


def _settle_session(
    record: TaskRecord,
    task_store: TaskStore,
    plane: Any,
    run_states: RunStateStore,
    *,
    prefetched: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, Any] | None]:
    """``_settle_task`` that also returns the aggregate reason (needed for
    the ``provisioning`` phase). Same read-path convergence contract."""
    ws = (
        prefetched.get("ws")
        if prefetched is not None
        else _tasks._ws_record(plane, record.agent_id)
    )
    agg_pre = None
    if prefetched is not None:
        agg_pre = {}
        if prefetched.get("rec") is not None:
            agg_pre["rec"] = prefetched["rec"]
        if "statuses" in prefetched:
            agg_pre["statuses"] = prefetched["statuses"]
    status, reason = _tasks._aggregate_status(record, run_states, plane, ws, prefetched=agg_pre)
    _converge_writeback(record, status, reason, task_store)
    return status, reason, ws


def _converge_writeback(
    record: TaskRecord,
    status: str,
    reason: str,
    task_store: TaskStore,
) -> None:
    """Persist a changed aggregate — the read-path convergence contract.

    A failed convergence write must not 500 a status read: the live
    aggregate is the answer either way."""
    if status == record.status:
        return
    record.transitions.append({"status": status, "reason": reason, "at": _iso_now()})
    record.status = status
    record.updated_at = _iso_now()
    try:
        task_store.put(record)
    except Exception:
        pass


def _terminal_projection(record: TaskRecord, ws: dict[str, Any] | None) -> tuple[str, str]:
    """Aggregate for a stored-terminal row without any live reads.

    ``error``/``cancelled``/``expired``/``delivery_failed`` are absorbing.
    ``finished`` is terminal but flippable only by the delivery outcome —
    which the workspace record already carries. A stored ``finished`` is
    itself the settled verdict of a FINISHED latest run; a newer queued /
    running run can only exist when a mutation path already rewrote the
    stored status, so the ledger read adds nothing (SOR-268 round 3 — it
    was the ~linear term on the list endpoint)."""
    if record.status != "finished":
        return record.status, "stored"
    delivery = _tasks._delivery_view(record, ws)
    if delivery is not None:
        if delivery["status"] == "failed":
            return "delivery_failed", "delivery_failed"
        if delivery["status"] != "delivered":
            return "delivering", "delivery_pending"
    return "finished", "run_finished"


def _status_aggregate(
    record: TaskRecord,
    ws: dict[str, Any] | None,
    run_states: RunStateStore,
    plane: Any,
) -> tuple[str, str]:
    """Cheap aggregate for the SSE status tick.

    A durable terminal task needs only its stored row plus workspace delivery
    state. Re-reading the live plane and run ledger adds no information and
    creates ambient Modal traffic that competes with unrelated API reads.
    """
    if record.status in _tasks._TASK_TERMINAL:
        return _terminal_projection(record, ws)
    return _tasks._aggregate_status(record, run_states, plane, ws)


def _view(
    record: TaskRecord,
    *,
    plane: Any,
    v1: V1State,
    run_states: RunStateStore,
    scheduler: Any,
    reporter: Any,
    task_store: TaskStore,
    settled: tuple[str, str, dict[str, Any] | None] | None = None,
    runs_v1: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Session projection with live extras (usage/cost/turns/error).

    ``settled`` is the caller's ``(status, reason, ws)`` triple when it
    already paid ``_settle_session``; otherwise it is paid here. ``runs_v1``
    is the caller's already-rendered run list when the detail path paid
    the ledger read once.
    """
    if settled is None:
        settled = _settle_session(record, task_store, plane, run_states)
    status, reason, ws = settled
    extras = live_extras(record, plane)
    if runs_v1 is None:
        runs_v1 = []
        rec = plane.get(record.agent_id) if record.agent_id else None
        if rec is not None:
            runs_v1 = _tasks._task_runs(plane, rec, v1, run_states, scheduler, reporter)
    return session_view(
        record,
        plane=plane,
        task_store=task_store,
        run_states=run_states,
        ws=ws,
        aggregate_status=status,
        aggregate_reason=reason,
        error=_session_error(record, status, runs_v1),
        usage=extras["usage"],
        cost_estimate_usd=extras["cost_estimate_usd"],
        turns=extras["turns"],
    )


def _detail(
    record: TaskRecord,
    *,
    plane: Any,
    v1: V1State,
    run_states: RunStateStore,
    scheduler: Any,
    reporter: Any,
    task_store: TaskStore,
) -> dict[str, Any]:
    """Bounded first view: session + its most recent runs.

    SOR-268: the detail read fans its remote reads (live record, workspace
    doc, ledger rows) out concurrently and renders every run off that one
    ledger list — the serial walk is what made cold detail reads take
    ~3.4-4.7s in the SOR-260 gate.
    """
    prefetched = _prefetch_reads(record, plane)
    settled = _settle_session(record, task_store, plane, run_states, prefetched=prefetched)
    rec = prefetched.get("rec")
    if rec is None and record.agent_id is not None:
        rec = plane.get(record.agent_id)
    runs_v1: list[dict[str, Any]] = []
    if rec is not None:
        runs_v1 = _tasks._task_runs(
            plane,
            rec,
            v1,
            run_states,
            scheduler,
            reporter,
            rows=prefetched.get("rows"),
        )
    session = _view(
        record,
        plane=plane,
        v1=v1,
        run_states=run_states,
        scheduler=scheduler,
        reporter=reporter,
        task_store=task_store,
        settled=settled,
        runs_v1=runs_v1,
    )
    runs = [run_view(r) for r in runs_v1]
    return {
        "session": session,
        "runs": runs[-RUNS_FIRST_VIEW_MAX:],
        "run_count": len(runs),
        "truncated": len(runs) > RUNS_FIRST_VIEW_MAX,
    }


def _to_task_request(body: CreateSessionRequest) -> _tasks.CreateTaskRequest:
    """Project the V2 declaration onto the V1 task request shape."""
    adv = body.advanced
    return _tasks.CreateTaskRequest(
        prompt=Prompt(text=body.prompt),
        name=body.title,
        source=(
            _tasks.TaskSource(repo=body.repository.repo, ref=body.repository.ref)
            if body.repository is not None
            else None
        ),
        execution=(
            _tasks.TaskExecution(**body.execution.model_dump())
            if body.execution is not None
            else None
        ),
        delivery=(
            _tasks.TaskDelivery(
                branch=body.delivery.branch,
                pull_request=(
                    _tasks.TaskPullRequest(**body.delivery.pull_request.model_dump())
                    if body.delivery.pull_request is not None
                    else None
                ),
                auto_publish=body.delivery.auto_publish,
            )
            if body.delivery is not None
            else None
        ),
        metadata=adv.metadata if adv else None,
        output_contract=adv.output_contract if adv else None,
        resources=adv.resources if adv else None,
        compute=adv.compute if adv else None,
        idle_timeout_s=adv.idle_timeout_s if adv else None,
    )


# ---------------------------------------------------------------------------
# bounded-ACK machinery (SOR-265)
# ---------------------------------------------------------------------------

_ACK_BUDGET_ENV = "SBX_V2_ACK_BUDGET_S"
# On a warm production path the dispatch work (resolve/provision/bind)
# never lands inside the wait window — the budget is then pure added
# latency on the ACK. 0.35s still surfaces fast validation failures
# (SOR-271: p95 ACK < 1s needs the window under ~400ms).
_DEFAULT_ACK_BUDGET_S = 0.35

# SOR-268: concurrent settles on the list endpoint — one remote batch
# per page row serialized into N × remote latency in the gate run.
_SETTLE_FANOUT = env_int("SBX_V2_SETTLE_FANOUT", 8)
# Frames per joined SSE yield — bounds event-loop send cycles per subscriber.
_SSE_BATCH = env_int("SBX_V2_SSE_BATCH", 128)

_PRE_BIND_CANCELLED: set[str] = set()
"""Session ids cancelled before the create worker bound an agent. The
worker re-checks after binding and cancels the just-landed agent so the
cancel always wins (SOR-265)."""


def _ack_budget(request: Request) -> float:
    """How long a mutating route may wait for its worker before answering
    with an optimistic view. ``app.state.v2_ack_budget_s`` overrides the env
    var for tests; both default to ``_DEFAULT_ACK_BUDGET_S``."""
    override = getattr(request.app.state, "v2_ack_budget_s", None)
    if override is not None:
        return max(0.0, float(override))
    try:
        return max(0.0, float(os.environ.get(_ACK_BUDGET_ENV, _DEFAULT_ACK_BUDGET_S)))
    except ValueError:
        return _DEFAULT_ACK_BUDGET_S


# SOR-271 round 2: the heavy request work (dispatch/cancel/retry chains of
# sequential dict + sandbox ops) runs on ONE bounded pool, not an unbounded
# thread per request. Under a 20× parallel burst the remote-op queue depth
# stays bounded, so a fast-path read (``_require_session`` get, ACK) never
# waits behind ~20 concurrent op chains on the shared store channel.
_V2_OPS_WORKERS = env_int("SBX_V2_OPS_WORKERS", 6)
_HEAVY_POOL = ThreadPoolExecutor(max_workers=_V2_OPS_WORKERS, thread_name_prefix="sbx-v2-ops")

# Persist cancel intent before ACK, then give a parallel burst's request-path
# writes a short head start before expensive stop/drain chains use the same
# Modal client channel.
_CANCEL_CONVERGE_DELAY_S = env_float("SBX_V2_CANCEL_CONVERGE_DELAY_S", 0.6)

# Repeated Session list reads (console polling + acceptance probes) should not
# repeatedly consume the shared Modal RPC channel. The rendered-page memo is
# deliberately short-lived and every local V2 mutation invalidates the
# owner's entries immediately.
_LIST_MEMO_TTL_S = env_float("SBX_V2_LIST_MEMO_S", 0.8)
_LIST_MEMO_MAX = 64
_LIST_MEMO_LOCK = threading.Lock()
_LIST_MEMO: dict[tuple[Any, str, int, int], tuple[float, dict[str, Any]]] = {}

# A post/retry request can time out its ACK while the accepted mutation is
# still running on ``_HEAVY_POOL``. During that window the durable task row
# may still look terminal even though new work has been committed. Track the
# local mutation explicitly so a concurrent cancel cannot replay the stale
# terminal row and lose the cancellation. The cancel request persists its
# durable marker first, then defers the real cancel until the mutation exits.
_MUTATION_LOCK = threading.Lock()
_MUTATIONS_INFLIGHT: dict[str, int] = {}
_CANCEL_AFTER_MUTATION: set[str] = set()


def _list_memo_get(
    task_store: TaskStore, owner: str, limit: int, offset: int
) -> dict[str, Any] | None:
    key = (task_store, owner, int(limit), int(offset))
    with _LIST_MEMO_LOCK:
        hit = _LIST_MEMO.get(key)
        if hit is None or time.monotonic() - hit[0] >= _LIST_MEMO_TTL_S:
            return None
        return hit[1]


def _list_memo_put(
    task_store: TaskStore,
    owner: str,
    limit: int,
    offset: int,
    result: dict[str, Any],
) -> None:
    key = (task_store, owner, int(limit), int(offset))
    with _LIST_MEMO_LOCK:
        if len(_LIST_MEMO) >= _LIST_MEMO_MAX and key not in _LIST_MEMO:
            _LIST_MEMO.clear()
        _LIST_MEMO[key] = (time.monotonic(), result)


def _list_memo_drop(task_store: TaskStore, owner: str) -> None:
    with _LIST_MEMO_LOCK:
        for key in [k for k in tuple(_LIST_MEMO) if k[0] is task_store and k[1] == owner]:
            _LIST_MEMO.pop(key, None)


def _mutation_begin(session_id: str) -> None:
    with _MUTATION_LOCK:
        _MUTATIONS_INFLIGHT[session_id] = _MUTATIONS_INFLIGHT.get(session_id, 0) + 1


def _mutation_active(session_id: str) -> bool:
    with _MUTATION_LOCK:
        return _MUTATIONS_INFLIGHT.get(session_id, 0) > 0


def _defer_cancel_if_mutating(session_id: str) -> bool:
    """Atomically arm a post-mutation cancel when work is still in flight."""
    with _MUTATION_LOCK:
        if _MUTATIONS_INFLIGHT.get(session_id, 0) <= 0:
            return False
        _CANCEL_AFTER_MUTATION.add(session_id)
        return True


def _mutation_finish(session_id: str) -> bool:
    """Drop one mutation lease; True means this worker owns deferred cancel."""
    with _MUTATION_LOCK:
        count = _MUTATIONS_INFLIGHT.get(session_id, 0)
        if count <= 1:
            _MUTATIONS_INFLIGHT.pop(session_id, None)
            if session_id in _CANCEL_AFTER_MUTATION:
                _CANCEL_AFTER_MUTATION.remove(session_id)
                return True
            return False
        _MUTATIONS_INFLIGHT[session_id] = count - 1
        return False


# Request-path prefetch staging (owner-doc reads the next store write
# will consume). Fire-and-forget — stores treat a staged row as a
# best-effort warm, never as required input.
_PREFETCH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sbx-v2-prefetch")


def _run_with_budget(fn: Any, budget_s: float) -> tuple[bool, dict[str, Any]]:
    """Submit ``fn`` to the bounded ops pool and wait up to ``budget_s``.

    Returns ``(completed, box)`` — ``box['result']``/``box['error']`` hold
    the outcome when ``completed``. The submitted work keeps running after
    a timeout (or waits out pool backpressure): the request path never
    serializes on resolve/provision/sandbox work, only on this bounded wait.
    """
    box: dict[str, Any] = {}
    done = threading.Event()

    def _go() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 — surfaced to the route
            box["error"] = exc
        finally:
            done.set()

    _HEAVY_POOL.submit(_go)
    return done.wait(max(0.0, budget_s)), box


def _dispatch_error(record: TaskRecord) -> dict[str, Any] | None:
    """Error detail left by a failed async dispatch (``dispatch_failed``
    transition) — the session-level ``error`` for sessions that never got
    an agent and therefore have no run error."""
    for transition in reversed(record.transitions):
        if transition.get("reason") == "dispatch_failed":
            detail = transition.get("detail")
            if isinstance(detail, dict):
                return detail
    return None


def _session_error(
    record: TaskRecord,
    aggregate_status: str,
    runs_v1: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """The session-level ``error`` — only while the session itself is failed.

    The current attempt's run error, else the dispatch failure for a
    session that never got an agent. A failed earlier attempt stays
    traceable on its own run row: a queued/running/finished retry never
    resurrects it at session level (B6).
    """
    if map_session_status(aggregate_status)[0] != "failed":
        return None
    if runs_v1:
        error = latest_run_error(runs_v1)
        if error is not None:
            return error
    return _dispatch_error(record)


def _mark_dispatch_failed(task_store: TaskStore, session_id: str, exc: BaseException) -> None:
    """Persist a terminal dispatch failure on a not-yet-bound record.

    Runs on the worker thread after ``_create_task_once`` raised: without
    this the session would sit ``queued`` forever with no agent.
    """
    record = task_store.get(session_id)
    if record is None or record.agent_id is not None or record.status in _tasks._TASK_TERMINAL:
        return
    if isinstance(exc, V1ApiError):
        spec = spec_for(exc.code, exc.status_code)
        detail: dict[str, Any] = {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable if exc.retryable is not None else spec.retryable,
        }
        if exc.retry_after is not None:
            detail["retry_after"] = exc.retry_after
    else:
        detail = {"code": "internal", "message": str(exc) or type(exc).__name__, "retryable": True}
    record.status = "error"
    record.transitions.append(
        {"status": "error", "reason": "dispatch_failed", "at": _iso_now(), "detail": detail}
    )
    record.updated_at = _iso_now()
    try:
        task_store.put(record)
    except Exception:
        pass


def _redrive_dispatch(
    record: TaskRecord,
    *,
    prompt: str | None,
    delivery_mode: bool,
    key: ApiKey,
    plane: Any,
    registry: Any,
    scheduler: Scheduler,
    v1: V1State,
    run_states: RunStateStore,
    workflows: Any,
    reporter: Any,
    resources_registry: Any,
    capabilities: Any,
    task_store: TaskStore,
    resolver: Any,
) -> dict[str, Any]:
    """Re-drive the create dispatch for a session that failed before an
    agent ever bound (SOR-271).

    ``retry_task`` re-posts the prompt to a bound agent — a pre-bind
    failure (concurrency_limit / account_unavailable / provider_exhausted)
    has none, so the only honest retry is the original create path:
    re-resolve, re-reserve, bind onto the same session id, start run-1.
    The bind replaces the record wholesale, so the failed attempts are
    folded into ``transitions`` first to keep the history traceable.
    """
    if record.status != "error":
        if record.status in _tasks._TASK_TERMINAL:
            raise V1ApiError(
                409, "task_not_retryable", "session is terminal — there is nothing to retry"
            )
        raise V1ApiError(409, "session_not_runnable", "session is still provisioning")
    if delivery_mode:
        raise V1ApiError(
            409, "task_not_retryable", "session never delivered — nothing to republish"
        )
    detail = _dispatch_error(record)
    if detail is None or detail.get("retryable") is not True:
        raise V1ApiError(409, "task_not_retryable", "session's dispatch failure is not retryable")
    spec = dict(record.request or {})
    if prompt is not None:
        spec["prompt"] = {"text": prompt}
    v1_body = _tasks.CreateTaskRequest(**spec)
    idem = record.idempotency or {}
    # Consume any pre-redrive cancel handshake, then re-mark the record
    # queued so the window between here and the bind reads honestly —
    # a cancel that lands during the dispatch re-adds itself to
    # ``_PRE_BIND_CANCELLED`` and is caught post-bind below.
    _PRE_BIND_CANCELLED.discard(record.id)
    record.transitions.append({"status": "queued", "reason": "retry_dispatch", "at": _iso_now()})
    record.status = "queued"
    record.updated_at = _iso_now()
    task_store.put(record)
    get_fresh = getattr(task_store, "get_fresh", None) or task_store.get
    fresh = get_fresh(record.id)
    if fresh is None or fresh.status == "cancelled":
        raise V1ApiError(409, "task_not_retryable", "session was cancelled")
    try:
        created = _tasks._create_task_once(
            v1_body,
            key,
            plane,
            registry,
            scheduler,
            v1,
            run_states,
            workflows,
            reporter=reporter,
            resources_registry=resources_registry,
            capabilities=capabilities,
            task_store=task_store,
            resolver=resolver,
            idempotency_key=idem.get("key"),
            idempotency_fingerprint=idem.get("fingerprint"),
            new_id=lambda: record.id,
        )
    except Exception as exc:
        _mark_dispatch_failed(task_store, record.id, exc)
        raise
    bound = task_store.get(record.id)
    if bound is not None:
        bound.transitions = [*record.transitions, *bound.transitions]
        task_store.put(bound)
    if record.id in _PRE_BIND_CANCELLED:
        # A cancel landed while the agent was being bound — cancel the
        # just-landed agent (same handshake as create_session).
        try:
            _tasks.cancel_task(
                record.id,
                key=key,
                plane=plane,
                v1=v1,
                run_states=run_states,
                workflows=workflows,
                scheduler=scheduler,
                reporter=reporter,
                task_store=task_store,
            )
        finally:
            _PRE_BIND_CANCELLED.discard(record.id)
    return created


def _optimistic_view(
    record: TaskRecord,
    *,
    aggregate_status: str,
    aggregate_reason: str,
    task_store: TaskStore,
    run_states: RunStateStore,
    plane: Any,
) -> dict[str, Any]:
    """A session view built from the durable record only — no workspace
    fetch, no settle writes, no sandbox access."""
    return session_view(
        record,
        plane=plane,
        task_store=task_store,
        run_states=run_states,
        ws=None,
        aggregate_status=aggregate_status,
        aggregate_reason=aggregate_reason,
        error=_session_error(record, aggregate_status),
    )


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@router.post("/sessions", status_code=201, response_model=SessionResponse)
def create_session(
    body: CreateSessionRequest,
    request: Request,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    registry: AccountRegistry = Depends(get_registry),
    scheduler: Scheduler = Depends(get_scheduler),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    reporter: Any = Depends(get_run_reporter),
    workflows: Any = Depends(get_workflow_service),
    resources_registry: Any = Depends(get_resources),
    capabilities: Any = Depends(get_capabilities),
    task_store: TaskStore = Depends(get_task_store),
    resolver: Any = Depends(get_repo_resolver),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Create a session: declare → resolve → reserve → first turn async.

    Same idempotency semantics as ``POST /v1/tasks`` — the pin is
    ``v2:session:<key>`` so a V1 task and a V2 session can never collide.
    """
    ack_deadline = time.monotonic() + _ack_budget(request)
    _list_memo_drop(task_store, key.id)
    v1_body = _to_task_request(body)
    fingerprint = request_fingerprint(v1_body)
    pin_key = f"v2:session:{idempotency_key}" if idempotency_key else None
    # SOR-268 round 3: the owner-doc read ``put`` is about to need is issued
    # up front on every create — not only the keyed path — so the
    # read-modify-write only pays its ``Dict.update`` (SOR-271). On the
    # keyed path ``find_by_idempotency`` stages the same row itself while
    # resolving the dedup read.
    prefetch = getattr(task_store, "prefetch_owner", None)
    if callable(prefetch):
        _PREFETCH_POOL.submit(prefetch, key.id)
    owned = None
    if pin_key is not None:
        outcome, entry = v1.idempotency.claim(key.id, pin_key, fingerprint)
        if outcome == "hit":
            return entry.body
        if outcome == "conflict":
            raise V1ApiError(
                409,
                "idempotency_conflict",
                "Idempotency-Key was already used with a different request body",
            )
        if outcome == "timeout":
            raise V1ApiError(
                409,
                "idempotency_in_progress",
                "a create with this Idempotency-Key is still in progress",
            )
        # ``find_by_idempotency`` issues the point index + pre-index owner
        # reads concurrently and stages the owner doc for the imminent
        # ``put`` — the whole dedup+stage is one wall round trip.
        # Order is preserved: ``put`` still runs only after the prior
        # check passes, so an idempotent replay never writes an orphan
        # session row.
        prior = task_store.find_by_idempotency(key.id, pin_key)
        if prior is not None:
            prior_fp = (prior.idempotency or {}).get("fingerprint")
            if prior_fp not in (None, fingerprint):
                v1.idempotency.abandon(key.id, pin_key, entry)
                raise V1ApiError(
                    409,
                    "idempotency_conflict",
                    "Idempotency-Key was already used with a different request body",
                )
            result = {
                "session": _view(
                    prior,
                    plane=plane,
                    v1=v1,
                    run_states=run_states,
                    scheduler=scheduler,
                    reporter=reporter,
                    task_store=task_store,
                )
            }
            v1.idempotency.complete(key.id, pin_key, entry, agent_id=prior.agent_id, body=result)
            v1.idempotency.settle(key.id, pin_key, entry)
            return result
        owned = entry

    # Persist the queued session up front: the public id exists at once and
    # projects to queued/provisioning while resolve/reserve/provision runs
    # off the request path (SOR-265). ``_create_task_once`` overwrites this
    # record with the bound one (``new_id`` pins the id) when it lands.
    record = TaskRecord(
        id=new_session_id(),
        owner=key.id,
        status="queued",
        request=_tasks._spec(v1_body),
        resolved=None,
        agent_id=None,
        run_id=None,
        created_at=_iso_now(),
        updated_at=_iso_now(),
        transitions=[{"status": "queued", "reason": "awaiting_dispatch", "at": _iso_now()}],
        idempotency=(
            {"key_id": key.id, "key": pin_key, "fingerprint": fingerprint} if pin_key else None
        ),
    )
    task_store.put(record)

    on_provisioned = None
    if owned is not None:
        on_provisioned = lambda: v1.idempotency.settle(  # noqa: E731
            key.id, pin_key, owned
        )

    def _dispatch() -> dict[str, Any]:
        try:
            created = _tasks._create_task_once(
                v1_body,
                key,
                plane,
                registry,
                scheduler,
                v1,
                run_states,
                workflows,
                reporter=reporter,
                resources_registry=resources_registry,
                capabilities=capabilities,
                task_store=task_store,
                resolver=resolver,
                idempotency_key=pin_key,
                idempotency_fingerprint=fingerprint,
                on_provisioned=on_provisioned,
                new_id=lambda: record.id,
            )
        except Exception as exc:
            _mark_dispatch_failed(task_store, record.id, exc)
            raise
        if record.id in _PRE_BIND_CANCELLED:
            # A cancel landed while the agent was still being bound — the
            # dispatch's last write wins the record, so cancel it again on
            # the now-bound row.
            try:
                _tasks.cancel_task(
                    record.id,
                    key=key,
                    plane=plane,
                    v1=v1,
                    run_states=run_states,
                    workflows=workflows,
                    scheduler=scheduler,
                    reporter=reporter,
                    task_store=task_store,
                )
            finally:
                _PRE_BIND_CANCELLED.discard(record.id)
        return created

    # The optional dispatch wait shares the route's budget with mandatory
    # dedup and persistence. Adding a fresh full wait after those RPCs made
    # warm keyed creates exceed the SLO even after history scans were fixed.
    done, box = _run_with_budget(_dispatch, max(0.0, ack_deadline - time.monotonic()))
    if done and "error" in box:
        if owned is not None:
            v1.idempotency.abandon(key.id, pin_key, owned)
        raise box["error"]
    # Once the wait expires, even a fresh task read (or a bound-session
    # render) can add unbounded remote work to the ACK. The queued row was
    # persisted before dispatch; reads will expose the worker's result.
    fresh = (task_store.get(record.id) or record) if done else record
    if fresh.agent_id is not None:
        result = {
            "session": _view(
                fresh,
                plane=plane,
                v1=v1,
                run_states=run_states,
                scheduler=scheduler,
                reporter=reporter,
                task_store=task_store,
            )
        }
    elif fresh.status in _tasks._TASK_TERMINAL:
        if owned is not None:
            v1.idempotency.abandon(key.id, pin_key, owned)
        result = {
            "session": _optimistic_view(
                fresh,
                aggregate_status=fresh.status,
                aggregate_reason="dispatch_failed",
                task_store=task_store,
                run_states=run_states,
                plane=plane,
            )
        }
    else:
        result = {
            "session": _optimistic_view(
                fresh,
                aggregate_status="queued",
                aggregate_reason="awaiting_dispatch",
                task_store=task_store,
                run_states=run_states,
                plane=plane,
            )
        }
    if owned is not None and fresh.status not in _tasks._TASK_TERMINAL:
        v1.idempotency.complete(
            key.id,
            pin_key,
            owned,
            agent_id=fresh.agent_id or record.id,
            body=result,
        )
    return result


@router.get("/sessions", response_model=SessionListResponse)
def list_sessions(
    key: ApiKey = Depends(agents_key),
    task_store: TaskStore = Depends(get_task_store),
    run_states: RunStateStore = Depends(get_run_states),
    plane: Any = Depends(get_plane),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """The caller's sessions, newest first."""
    memo = _list_memo_get(task_store, key.id, limit, offset)
    if memo is not None:
        return memo
    records = [r for r in task_store.list(key.id) if is_session_id(r.id)]
    records.sort(key=lambda r: (r.created_at, r.id), reverse=True)
    total = len(records)
    page = records[offset : offset + limit]
    # SOR-268: a settle pays a few cached/pooled remote reads (plane.get,
    # run_states, ws) — serialized, the page was N × remote latency (the
    # SOR-260 B2 finding). Bound the fanout so the pool can't itself
    # become a thundering herd on the remote store.
    # SOR-271 round 2: workspace rows come from ONE index-doc read, and
    # rows already at a durable terminal status skip the remote settle —
    # the page cost tracks live rows, never the full history.
    # SOR-268 round 3: ``finished`` joins that skip — the only live input
    # that can still change it is the delivery outcome, which ``ws_map``
    # already carries; the per-row ``plane.get`` + ledger list was the
    # residual ~linear term the re-gate still measured @333 rows.
    ws_map: dict[str, Any] = {}
    workspaces = getattr(plane, "workspaces", None)
    # ``plane.workspaces`` is the service; the index-backed listing lives
    # on the store it wraps (``list_records`` on either, duck-typed).
    if not callable(getattr(workspaces, "list_records", None)):
        workspaces = getattr(workspaces, "store", None)
    list_records = getattr(workspaces, "list_records", None)
    if callable(list_records):
        try:
            ws_map = {
                str(agent_id): raw for agent_id, raw in list_records() if isinstance(raw, dict)
            }
        except Exception:
            ws_map = {}

    def _settle_row(record: TaskRecord) -> tuple[str, str, dict[str, Any] | None]:
        ws = ws_map.get(record.agent_id) if record.agent_id else None
        if record.status in _tasks._TASK_TERMINAL:
            # Terminal rows project from the stored row + ws map alone —
            # converging write-back only when the delivery flip actually
            # applies (a rare bounded write, not the per-row live reads).
            status, reason = _terminal_projection(record, ws)
            _converge_writeback(record, status, reason, task_store)
            return status, reason, ws
        prefetched = {"ws": ws, "rec": None, "rows": None}
        return _settle_session(record, task_store, plane, run_states, prefetched=prefetched)

    if page:
        with ThreadPoolExecutor(
            max_workers=_SETTLE_FANOUT, thread_name_prefix="sbx-v2-settle"
        ) as pool:
            settled = list(pool.map(_settle_row, page))
    else:
        settled = []
    sessions = [
        session_view(
            record,
            plane=plane,
            task_store=task_store,
            run_states=run_states,
            ws=ws,
            aggregate_status=status,
            aggregate_reason=reason,
            error=_session_error(record, status),
        )
        for record, (status, reason, ws) in zip(page, settled)
    ]
    result = {"sessions": sessions, "total": total, "limit": limit, "offset": offset}
    _list_memo_put(task_store, key.id, limit, offset, result)
    return result


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
def get_session(
    session_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    workflows: Any = Depends(get_workflow_service),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: Any = Depends(get_run_reporter),
    task_store: TaskStore = Depends(get_task_store),
) -> dict[str, Any]:
    """The bounded first view: session + its most recent runs, one call."""
    record = _require_session(task_store, key, session_id)
    return _detail(
        record,
        plane=plane,
        v1=v1,
        run_states=run_states,
        scheduler=scheduler,
        reporter=reporter,
        task_store=task_store,
    )


@router.get("/sessions/{session_id}/history")
def get_session_history(
    session_id: str,
    request: Request,
    before_n: int | None = Query(default=None, ge=1),
    limit: int = Query(default=10, ge=1, le=25),
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: Any = Depends(get_run_reporter),
    task_store: TaskStore = Depends(get_task_store),
) -> dict[str, Any]:
    """Older turns and their durable activity, fetched only on demand.

    Cursor is a turn number, so newly appended turns do not shift pages.
    The existing detail and event contracts are unchanged.
    """
    from control.api_v2.events import normalize

    record = _require_session(task_store, key, session_id)
    rec = plane.get(record.agent_id) if record.agent_id else None
    rows = _tasks._task_runs(plane, rec, v1, run_states, scheduler, reporter) if rec else []
    rows = [run_view(r) for r in rows]
    eligible = sorted(
        (r for r in rows if before_n is None or int(r["n"]) < before_n),
        key=lambda r: int(r["n"]),
    )
    page = eligible[-limit:]
    activity = getattr(request.app.state, "run_activity", None)
    events: list[dict[str, Any]] = []
    if activity is not None and record.agent_id:
        for row in page:
            n = int(row["n"])
            for entry in activity.get(record.agent_id, n) or []:
                frame = normalize(entry["event"], n)
                if frame is not None:
                    events.append({"id": entry["id"], "event": frame})
    return {
        "runs": page,
        "events": events,
        "has_more": len(eligible) > len(page),
        "next_before_n": int(page[0]["n"]) if page else None,
    }


@router.post(
    "/sessions/{session_id}/messages",
    status_code=202,
    response_model=SessionMessageResponse,
)
def post_message(
    session_id: str,
    body: SessionMessageRequest,
    request: Request,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: Any = Depends(get_run_reporter),
    workflows: Any = Depends(get_workflow_service),
    task_store: TaskStore = Depends(get_task_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Queue a follow-up turn; long provider work stays async (202).

    SOR-265: ``create_task_run`` serializes ``reconcile_turn`` /
    ``_dispatch_turn`` sandbox ops before returning, so it runs on a worker
    under ``_ack_budget``. The cheap refusals (dead agent, terminal agent,
    busy + ``on_busy=reject``) stay synchronous so the route never lies
    202; a timeout answers ``message: null`` — accepted, allocation still
    landing — which the schema already permits.
    """
    _list_memo_drop(task_store, key.id)
    record = _require_session(task_store, key, session_id)
    agent_id = record.agent_id
    if agent_id is None:
        raise V1ApiError(409, "session_not_runnable", "session is still provisioning")
    rec = plane.get(agent_id)
    if rec is None or rec.status in TERMINAL_STATUSES:
        raise V1ApiError(409, "session_not_runnable", "session's agent is not runnable")
    if body.on_busy == "reject" and (
        rec.status == "running"
        or rec.current_turn_id is not None
        or agent_id in getattr(plane, "_first_turn_pending", ())
    ):
        raise V1ApiError(
            409,
            "turn_in_progress",
            "turn_in_progress",
            retry_after=plane.turn_max_seconds,
        )

    v1_body = _tasks.CreateTaskRunRequest(prompt=Prompt(text=body.prompt), on_busy=body.on_busy)

    def _apply_deferred_cancel() -> None:
        try:
            _tasks.cancel_task(
                session_id,
                key=key,
                plane=plane,
                v1=v1,
                run_states=run_states,
                workflows=workflows,
                scheduler=scheduler,
                reporter=reporter,
                task_store=task_store,
            )
        except Exception:
            return
        mark_applied = getattr(task_store, "mark_cancel_applied", None)
        if callable(mark_applied):
            try:
                mark_applied(session_id)
            except Exception:
                pass

    def _post() -> dict[str, Any]:
        try:
            return _tasks.create_task_run(
                session_id,
                v1_body,
                key=key,
                plane=plane,
                v1=v1,
                run_states=run_states,
                scheduler=scheduler,
                reporter=reporter,
                workflows=workflows,
                task_store=task_store,
                idempotency_key=idempotency_key,
            )
        finally:
            if _mutation_finish(session_id):
                _apply_deferred_cancel()

    _mutation_begin(session_id)
    done, box = _run_with_budget(_post, _ack_budget(request))
    if done:
        if "error" in box:
            raise box["error"]
        result = box["result"]
        record_now = task_store.get(session_id) or record
        run = run_view(result["run"]) if result.get("run") else None
        # Same bounded-ACK rule as cancel: the worker converged the record
        # — one point read, no live view rebuild on the request path.
        return {
            "session": _optimistic_view(
                record_now,
                aggregate_status=record_now.status,
                aggregate_reason="stored",
                task_store=task_store,
                run_states=run_states,
                plane=plane,
            ),
            "message": ({"n": run["n"], "status": run["status"]} if run else None),
        }
    agg = record.status if record.status in ("running", "delivering") else "queued"
    return {
        "session": _optimistic_view(
            record,
            aggregate_status=agg,
            aggregate_reason=("stored" if agg != "queued" else "queued_work"),
            task_store=task_store,
            run_states=run_states,
            plane=plane,
        ),
        "message": None,
    }


@router.post("/sessions/{session_id}/cancel", response_model=SessionResponse)
def cancel_session(
    session_id: str,
    request: Request,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    workflows: Any = Depends(get_workflow_service),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: Any = Depends(get_run_reporter),
    task_store: TaskStore = Depends(get_task_store),
) -> dict[str, Any]:
    """Cancel outstanding work — queued turns drop, a running turn stops.

    SOR-265: ``cancel_task`` can serialize a ``plane.stop`` (reconcile +
    proc kill + stop-hook exec), so it runs on a worker under
    ``_ack_budget``; a timeout answers cancelled-optimistically since the
    worker still converges the record to ``cancelled``.
    """
    _list_memo_drop(task_store, key.id)
    record = _require_session_for_cancel_ack(task_store, key, session_id)
    if record.status in _tasks._TASK_TERMINAL and not _mutation_active(session_id):
        return {
            "session": _optimistic_view(
                record,
                aggregate_status=record.status,
                aggregate_reason="stored",
                task_store=task_store,
                run_states=run_states,
                plane=plane,
            )
        }
    if record.agent_id is None and record.status != "cancelled":
        # Provisional bind may still land — make the dispatch worker's
        # post-bind check cancel the agent it just created. ``error`` is
        # included: a retry re-dispatch may be in flight on this row.
        _PRE_BIND_CANCELLED.add(session_id)

    def _cancel() -> dict[str, Any]:
        return _tasks.cancel_task(
            session_id,
            key=key,
            plane=plane,
            v1=v1,
            run_states=run_states,
            workflows=workflows,
            scheduler=scheduler,
            reporter=reporter,
            task_store=task_store,
        )

    # Modal-backed task stores expose a one-RPC intent marker. Persist it
    # synchronously *before* returning the optimistic cancelled ACK, then run
    # the expensive cancel chain in the bounded worker pool. A failed marker
    # write falls back to the original budgeted path — never a false-success
    # fast path.
    mark_pending = getattr(task_store, "mark_cancel_pending", None)
    if callable(mark_pending):
        try:
            marked = bool(mark_pending(session_id))
        except Exception:
            marked = False
        if marked:
            # If a just-ACKed post/retry is still committing its new run,
            # do not race a cancel worker against it. The mutation worker's
            # ``finally`` owns the cancellation once it exits.
            if _defer_cancel_if_mutating(session_id):
                return {
                    "session": _optimistic_view(
                        record,
                        aggregate_status="cancelled",
                        aggregate_reason="task_cancelled",
                        task_store=task_store,
                        run_states=run_states,
                        plane=plane,
                    )
                }
            mark_applied = getattr(task_store, "mark_cancel_applied", None)

            def _converge() -> None:
                if _CANCEL_CONVERGE_DELAY_S > 0:
                    time.sleep(_CANCEL_CONVERGE_DELAY_S)
                try:
                    _cancel()
                except Exception:
                    # Pending remains visible; a repeated cancel can safely
                    # resubmit convergence. Do not claim applied on failure.
                    return
                if callable(mark_applied):
                    try:
                        mark_applied(session_id)
                    except Exception:
                        pass

            _HEAVY_POOL.submit(_converge)
            return {
                "session": _optimistic_view(
                    record,
                    aggregate_status="cancelled",
                    aggregate_reason="task_cancelled",
                    task_store=task_store,
                    run_states=run_states,
                    plane=plane,
                )
            }

    # Marker unavailable/failed: retain the legacy budgeted cancel semantics,
    # but still arm the same-process handoff when a post/retry is in flight.
    # That way a fast terminal replay from the legacy attempt cannot lose the
    # cancel once the accepted mutation finally commits. A process crash in
    # this fallback lane has exactly the pre-marker durability exposure.
    _defer_cancel_if_mutating(session_id)

    done, box = _run_with_budget(_cancel, _ack_budget(request))
    if done and "error" in box:
        raise box["error"]
    # SOR-268 round 3: the ACK never pays a live ``_view`` rebuild — the
    # worker already converged the record, so one point read answers with
    # the post-cancel state. A full ``_view`` here re-ran settle + ledger +
    # workspace reads on the request path and pushed bulk-cancel ACK p95
    # over budget on the SOR-271 re-gate.
    record_now = (task_store.get(session_id) or record) if done else record
    agg = record_now.status if record_now.status in _tasks._TASK_TERMINAL else "cancelled"
    return {
        "session": _optimistic_view(
            record_now,
            aggregate_status=agg,
            aggregate_reason=("stored" if agg != "cancelled" else "task_cancelled"),
            task_store=task_store,
            run_states=run_states,
            plane=plane,
        )
    }


@router.post("/sessions/{session_id}/retry", response_model=SessionRetryResponse)
def retry_session(
    session_id: str,
    request: Request,
    body: SessionRetryRequest | None = None,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    workflows: Any = Depends(get_workflow_service),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: Any = Depends(get_run_reporter),
    task_store: TaskStore = Depends(get_task_store),
    registry: AccountRegistry = Depends(get_registry),
    resources_registry: Any = Depends(get_resources),
    capabilities: Any = Depends(get_capabilities),
    resolver: Any = Depends(get_repo_resolver),
) -> dict[str, Any]:
    """Retry the failed step — a failed delivery re-publishes, a terminal
    run verdict re-runs the original (or overridden) prompt.

    A session that failed before its agent ever bound has nothing to
    re-post to — its retryable dispatch failure is re-driven through the
    original create path on the same session id (SOR-271).

    SOR-268: ``retry_task`` serializes settle + cancel/dispatch work, so
    it runs on a worker under ``_ack_budget``; a timeout answers
    ``run: null`` — accepted, the retry still landing — which the schema
    permits.
    """
    _list_memo_drop(task_store, key.id)
    record = _require_session(task_store, key, session_id)
    v1_body = _tasks.RetryTaskRequest(
        mode=(body.mode if body else None),
        prompt=(Prompt(text=body.prompt) if body and body.prompt is not None else None),
        on_busy=(body.on_busy if body else "queue"),
    )

    def _apply_deferred_cancel() -> None:
        try:
            _tasks.cancel_task(
                session_id,
                key=key,
                plane=plane,
                v1=v1,
                run_states=run_states,
                workflows=workflows,
                scheduler=scheduler,
                reporter=reporter,
                task_store=task_store,
            )
        except Exception:
            return
        mark_applied = getattr(task_store, "mark_cancel_applied", None)
        if callable(mark_applied):
            try:
                mark_applied(session_id)
            except Exception:
                pass

    def _retry() -> dict[str, Any]:
        try:
            fresh = task_store.get(session_id) or record
            if fresh.agent_id is None:
                return _redrive_dispatch(
                    fresh,
                    prompt=(body.prompt if body else None),
                    delivery_mode=(body.mode == "delivery" if body else False),
                    key=key,
                    plane=plane,
                    registry=registry,
                    scheduler=scheduler,
                    v1=v1,
                    run_states=run_states,
                    workflows=workflows,
                    reporter=reporter,
                    resources_registry=resources_registry,
                    capabilities=capabilities,
                    task_store=task_store,
                    resolver=resolver,
                )
            return _tasks.retry_task(
                session_id,
                v1_body,
                key=key,
                plane=plane,
                v1=v1,
                run_states=run_states,
                workflows=workflows,
                scheduler=scheduler,
                reporter=reporter,
                task_store=task_store,
            )
        finally:
            if _mutation_finish(session_id):
                _apply_deferred_cancel()

    _mutation_begin(session_id)
    done, box = _run_with_budget(_retry, _ack_budget(request))
    if done:
        if "error" in box:
            raise box["error"]
        result = box["result"]
        record_now = task_store.get(session_id) or record
        return {
            "session": _optimistic_view(
                record_now,
                aggregate_status=record_now.status,
                aggregate_reason="stored",
                task_store=task_store,
                run_states=run_states,
                plane=plane,
            ),
            "run": (run_view(result["run"]) if result.get("run") else None),
        }
    return {
        "session": _optimistic_view(
            record,
            aggregate_status=(
                record.status if record.status in ("running", "delivering") else "queued"
            ),
            aggregate_reason="queued_work",
            task_store=task_store,
            run_states=run_states,
            plane=plane,
        ),
        "run": None,
    }


@router.get("/sessions/{session_id}/changes", response_model=SessionChangesResponse)
def session_changes(
    session_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    workflows: Any = Depends(get_workflow_service),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: Any = Depends(get_run_reporter),
    task_store: TaskStore = Depends(get_task_store),
    revisions: Any = Depends(get_revisions),
) -> dict[str, Any]:
    """The session's produced changes: workspace diff state + revisions."""
    record = _require_session(task_store, key, session_id)
    session = _view(
        record,
        plane=plane,
        v1=v1,
        run_states=run_states,
        scheduler=scheduler,
        reporter=reporter,
        task_store=task_store,
    )
    rows = revisions.list(record.agent_id) if record.agent_id else []
    return {
        "session": session,
        "changes": session["changes"],
        "revisions": [revision_view(r.public()) for r in rows],
    }


@router.get(
    "/sessions/{session_id}/changes/diff",
    response_model=SessionChangesDiffResponse,
)
def session_changes_diff(
    session_id: str,
    n: int | None = Query(default=None),
    path: str | None = Query(default=None),
    key: ApiKey = Depends(agents_key),
    task_store: TaskStore = Depends(get_task_store),
    revisions: Any = Depends(get_revisions),
    artifacts: Any = Depends(get_artifact_store),
) -> dict[str, Any]:
    """The file-level view of a revision's durable patch — the Changes
    tab's file list + per-file diff (SOR-259).

    Serves the already-materialized ``patch.diff`` of the latest ready
    revision (or revision ``n``): parsed per-file paths/status/+/- counts
    by default, and one file's diff section when ``path`` is given so the
    console can lazy-load diffs without embedding them in the first view.
    """
    record = _require_session(task_store, key, session_id)
    rows = revisions.list(record.agent_id) if record.agent_id else []
    ready = [r for r in rows if r.status == "ready"]
    if n is not None:
        rev = next((r for r in ready if r.n == n), None)
    else:
        rev = ready[-1] if ready else None
    if rev is None or not rev.artifact_id:
        raise V1ApiError(404, "revision_not_found", "no materialized revision to diff")
    try:
        patch = artifacts.read(rev.artifact_id, "patch.diff").decode("utf-8", "replace")
    except ArtifactError as exc:
        raise V1ApiError(
            400, "artifact_invalid", "revision artifact is unavailable or corrupt"
        ) from exc
    files = parse_unified_diff(patch)
    totals = {
        "files_changed": len(files),
        "additions": sum(f.additions for f in files),
        "deletions": sum(f.deletions for f in files),
    }
    if path is not None:
        files = [f for f in files if f.path == path]
        if not files:
            raise not_found(f"no diff for file {path!r}")
        include_body = True
    else:
        include_body = False
    return {
        "n": rev.n,
        "base_sha": rev.base_sha,
        "head_sha": rev.head_sha,
        **totals,
        "files": [
            {
                "path": f.path,
                "status": f.status,
                "additions": f.additions,
                "deletions": f.deletions,
                "old_path": f.old_path,
                "diff": f.body if include_body else None,
            }
            for f in files
        ],
    }


@router.post("/sessions/{session_id}/deliver", response_model=SessionDeliverResponse)
def deliver_session(
    session_id: str,
    body: SessionDeliverRequest | None = None,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    workflows: Any = Depends(get_workflow_service),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: Any = Depends(get_run_reporter),
    task_store: TaskStore = Depends(get_task_store),
    revisions: Any = Depends(get_revisions),
) -> dict[str, Any]:
    """Deliver the session's changes — durable publish on the revision, so
    it works even after the author sandbox is gone."""
    _list_memo_drop(task_store, key.id)
    _require_session(task_store, key, session_id)
    v1_body = _revisions_api.DeliverRequest(
        revision=(str(body.n) if body is not None and body.n is not None else None),
        branch=(body.branch if body else None),
        pull_request=(
            _revisions_api.RevisionPullRequest(**body.pull_request.model_dump())
            if body is not None and body.pull_request is not None
            else None
        ),
    )
    result = _revisions_api.deliver_task(
        session_id, v1_body, key=key, task_store=task_store, revisions=revisions
    )
    record = task_store.get(session_id)
    return {
        "session": _view(
            record,
            plane=plane,
            v1=v1,
            run_states=run_states,
            scheduler=scheduler,
            reporter=reporter,
            task_store=task_store,
        ),
        "revision": revision_view(result["revision"]),
    }


# ------------------------------------------------------------------- SSE


@router.get("/sessions/{session_id}/events")
def stream_session_events(
    request: Request,
    session_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    after_n: int = Query(default=0, ge=0),
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    task_store: TaskStore = Depends(get_task_store),
) -> Any:
    """Session-scoped normalized event stream (SSE, reconnectable).

    Frames use the ``/api/*`` contract — ``id:`` the events.jsonl line
    number, ``event:`` the normalized Session-event type, ``data:`` the
    payload — with ``Last-Event-ID`` resume and ``: keepalive``. A fresh
    ``session.status`` frame (no id) opens every connection so a reconnect
    immediately learns the current state.

    SOR-271 round 2: this handler is SYNC — ``_require_session``,
    ``acquire``, ``subscribe`` and ``opening_status`` all run remote Dict
    reads; as an ``async def`` they executed on the ASGI event loop, so
    every connect (and every EOF reconnect herd of 20 clients) stalled
    the loop for hundreds of ms each and starved unrelated reads (the
    >5s read spikes the gate saw under live-session SSE fanout). A ``def``
    handler runs on the worker pool; only the queue-draining generator
    below stays on the loop.
    """
    from control.app import DisconnectAwareStreamingResponse

    record = _require_session(task_store, key, session_id)
    try:
        last_id = int(last_event_id) if last_event_id else 0
    except ValueError:
        last_id = 0
    start_line = max(1, last_id + 1)

    backend = getattr(plane, "backend", None)
    keepalive_s: float = getattr(request.app.state, "keepalive_s", 15.0)
    app_state = request.app.state

    def _status_bits() -> tuple[str, str, str]:
        """Current (status, phase, frame) — polled so transitions stream live."""
        fresh = task_store.get(session_id) or record
        ws = _tasks._ws_record(plane, fresh.agent_id)
        status, reason = _status_aggregate(fresh, ws, run_states, plane)
        st, ph = map_session_status(status, reason)
        payload = {"type": "session.status", "status": st, "phase": ph}
        return st, ph, f"event: session.status\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _bound_agent_id() -> str | None:
        fresh = task_store.get(session_id)
        return (fresh.agent_id if fresh is not None else None) or record.agent_id

    def _live_handle() -> tuple[Any, Any]:
        agent_id = _bound_agent_id()
        if backend is None or agent_id is None:
            return None, None
        rec_now = plane.get(agent_id)
        if rec_now is None:
            return None, None
        h = rec_now.handle()
        if h is None:
            return None, None
        try:
            p = backend.poll(h)
        except Exception:
            p = None
        return h, p

    def _agent_terminal() -> bool:
        agent_id = _bound_agent_id()
        if agent_id is None:
            return False
        rec_now = plane.get(agent_id)
        if rec_now is None or rec_now.status in ("closed", "timed_out", "lost"):
            return True
        statuses = _tasks._run_statuses_for(agent_id, run_states, plane)
        return bool(statuses) and all(s in RUN_TERMINAL for _, s in statuses)

    def _start_tail(handle: Any) -> Any:
        return backend.exec(
            handle,
            ["tail", "-n", "+1", "-F", "-s", "0.02", str(handle.root / "events.jsonl")],
            sandbox_env(handle),
        )

    def _replay_lines() -> list[str]:
        handle, _poll = _live_handle()
        if backend is None or handle is None:
            return []
        try:
            text = read_text(backend, handle, "events.jsonl")
        except Exception:
            return []
        return text.splitlines() if text else []

    def _replay_entries() -> list[dict[str, Any]]:
        """Sandbox gone: stitch the durable per-run transcripts."""
        activity = getattr(app_state, "run_activity", None)
        if activity is None:
            return []
        agent_id = _bound_agent_id()
        if agent_id is None:
            return []
        ns: set[int] = set()
        rec_now = plane.get(agent_id)
        if rec_now is not None:
            ns = _routes._known_run_ns(rec_now, _routes._ledger(plane), run_states)
        else:
            ledger = _routes._ledger(plane)
            if ledger is not None:
                try:
                    ns = {r.n for r in ledger.list(agent_id)}
                except Exception:
                    ns = set()
        entries: list[dict[str, Any]] = []
        for n in sorted(ns):
            try:
                entries.extend(activity.get(agent_id, n))
            except Exception:
                pass
        return entries

    # SOR-268: one hub per session owns the tail exec, the status settle,
    # and the provisioning/replay states — N clients are N queues on the
    # same poller, not N pollers (the SOR-260 B3 multiplier). The client
    # generator only drains its queue; nothing remote runs on the loop.
    registry = getattr(app_state, "v2_events_hubs", None)
    if registry is None:
        registry = _hub_mod.SessionEventsHubRegistry()
        app_state.v2_events_hubs = registry
    hub = registry.acquire(
        session_id,
        lambda: _hub_mod.SessionEventsHub(
            session_id,
            record_probe=lambda: task_store.get(session_id),
            is_terminal_record=lambda r: r.status in _tasks._TASK_TERMINAL,
            status_bits=_status_bits,
            live_handle=_live_handle,
            agent_terminal=_agent_terminal,
            start_tail=_start_tail,
            replay_lines=_replay_lines,
            replay_entries=_replay_entries,
        ),
    )
    sub, opening, backlog = hub.subscribe(start_line)
    if opening is None:
        opening = hub.opening_status()

    def visible(frame: str) -> bool:
        if not after_n:
            return True
        for line in frame.splitlines():
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                return int(payload.get("n") or after_n + 1) > after_n
        return True

    async def gen() -> AsyncIterator[str]:
        wake = asyncio.Event()
        loop = asyncio.get_running_loop()

        def notify() -> None:
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                pass  # disconnected client loop has already closed

        sub.wake = notify
        try:
            yield ": keepalive\n\n"
            if opening is not None:
                yield opening
            # Frames are independent SSE chunks — joining a backlog (or a
            # drained queue burst) into one yield keeps the wire format
            # identical while cutting event-loop send cycles from
            # O(frames × subscribers) to O(batches × subscribers). A 20-
            # client reconnect herd re-ingesting a long transcript no
            # longer floods the loop with per-frame sends (SOR-271 r2).
            for i in range(0, len(backlog), _SSE_BATCH):
                yield "".join(f for f in backlog[i : i + _SSE_BATCH] if visible(f))
            next_ka = time.monotonic() + keepalive_s
            while True:
                # Clear before draining: arrival between drain and wait sets
                # the event, so no frame can wait until the keepalive deadline.
                wake.clear()
                items: list[Any] = []
                try:
                    while True:
                        items.append(sub.q.get_nowait())
                except queue.Empty:
                    pass
                if not items:
                    now = time.monotonic()
                    if now >= next_ka:
                        yield ": keepalive\n\n"
                        next_ka = now + keepalive_s
                    try:
                        await asyncio.wait_for(wake.wait(), max(0.001, next_ka - now))
                    except TimeoutError:
                        pass
                    continue
                closing = False
                out: list[str] = []
                for item in items:
                    if item is _hub_mod._CLOSE:
                        closing = True
                        break
                    if visible(item):
                        out.append(item)
                if out:
                    yield "".join(out)
                if closing:
                    return
        finally:
            sub.wake = None
            hub.unsubscribe(sub)

    return DisconnectAwareStreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
