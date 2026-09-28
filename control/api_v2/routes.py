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
from control.api_v2 import events as _events
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
from control.config import TERMINAL_STATUSES
from control.ports import AccountRegistry, ApiKey, Scheduler
from control.sandbox_io import read_text, sandbox_env
from control.service import format_sse
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


def _settle_session(
    record: TaskRecord,
    task_store: TaskStore,
    plane: Any,
    run_states: RunStateStore,
) -> tuple[str, str, dict[str, Any] | None]:
    """``_settle_task`` that also returns the aggregate reason (needed for
    the ``provisioning`` phase). Same read-path convergence contract."""
    ws = _tasks._ws_record(plane, record.agent_id)
    status, reason = _tasks._aggregate_status(record, run_states, plane, ws)
    if status != record.status:
        record.transitions.append({"status": status, "reason": reason, "at": _iso_now()})
        record.status = status
        record.updated_at = _iso_now()
        try:
            task_store.put(record)
        except Exception:
            pass
    return status, reason, ws


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
) -> dict[str, Any]:
    """Session projection with live extras (usage/cost/turns/error).

    ``settled`` is the caller's ``(status, reason, ws)`` triple when it
    already paid ``_settle_session``; otherwise it is paid here.
    """
    if settled is None:
        settled = _settle_session(record, task_store, plane, run_states)
    status, reason, ws = settled
    extras = live_extras(record, plane)
    runs_v1: list[dict[str, Any]] = []
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
        error=latest_run_error(runs_v1) or _dispatch_error(record),
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
    """Bounded first view: session + its most recent runs, one round-trip."""
    settled = _settle_session(record, task_store, plane, run_states)
    session = _view(
        record,
        plane=plane,
        v1=v1,
        run_states=run_states,
        scheduler=scheduler,
        reporter=reporter,
        task_store=task_store,
        settled=settled,
    )
    runs_v1: list[dict[str, Any]] = []
    rec = plane.get(record.agent_id) if record.agent_id else None
    if rec is not None:
        runs_v1 = _tasks._task_runs(plane, rec, v1, run_states, scheduler, reporter)
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
_DEFAULT_ACK_BUDGET_S = 0.5

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


def _run_with_budget(fn: Any, budget_s: float) -> tuple[bool, dict[str, Any]]:
    """Run ``fn`` on a daemon thread and wait up to ``budget_s``.

    Returns ``(completed, box)`` — ``box['result']``/``box['error']`` hold
    the outcome when ``completed``. The thread keeps running after a
    timeout: the request path never serializes on resolve/provision/sandbox
    work, only on this bounded wait.
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

    threading.Thread(target=_go, daemon=True, name="sbx-v2-ack").start()
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
        error=_dispatch_error(record),
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
    v1_body = _to_task_request(body)
    fingerprint = request_fingerprint(v1_body)
    pin_key = f"v2:session:{idempotency_key}" if idempotency_key else None
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

    done, box = _run_with_budget(_dispatch, _ack_budget(request))
    if done and "error" in box:
        if owned is not None:
            v1.idempotency.abandon(key.id, pin_key, owned)
        raise box["error"]
    fresh = task_store.get(record.id) or record
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
    records = [r for r in task_store.list(key.id) if is_session_id(r.id)]
    records.sort(key=lambda r: (r.created_at, r.id), reverse=True)
    total = len(records)
    page = records[offset : offset + limit]
    sessions = []
    for record in page:
        status, reason, ws = _settle_session(record, task_store, plane, run_states)
        sessions.append(
            session_view(
                record,
                plane=plane,
                task_store=task_store,
                run_states=run_states,
                ws=ws,
                aggregate_status=status,
                aggregate_reason=reason,
            )
        )
    return {"sessions": sessions, "total": total, "limit": limit, "offset": offset}


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

    def _post() -> dict[str, Any]:
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

    done, box = _run_with_budget(_post, _ack_budget(request))
    if done:
        if "error" in box:
            raise box["error"]
        result = box["result"]
        record_now = task_store.get(session_id) or record
        run = run_view(result["run"]) if result.get("run") else None
        return {
            "session": _view(
                record_now,
                plane=plane,
                v1=v1,
                run_states=run_states,
                scheduler=scheduler,
                reporter=reporter,
                task_store=task_store,
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
    record = _require_session(task_store, key, session_id)
    if record.agent_id is None and record.status not in _tasks._TASK_TERMINAL:
        # Provisional bind may still land — make the dispatch worker's
        # post-bind check cancel the agent it just created.
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

    done, box = _run_with_budget(_cancel, _ack_budget(request))
    if done:
        if "error" in box:
            raise box["error"]
        record_now = task_store.get(session_id) or record
        return {
            "session": _view(
                record_now,
                plane=plane,
                v1=v1,
                run_states=run_states,
                scheduler=scheduler,
                reporter=reporter,
                task_store=task_store,
            )
        }
    agg = record.status if record.status in _tasks._TASK_TERMINAL else "cancelled"
    return {
        "session": _optimistic_view(
            record,
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
    body: SessionRetryRequest | None = None,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    workflows: Any = Depends(get_workflow_service),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: Any = Depends(get_run_reporter),
    task_store: TaskStore = Depends(get_task_store),
) -> dict[str, Any]:
    """Retry the failed step — a failed delivery re-publishes, a terminal
    run verdict re-runs the original (or overridden) prompt."""
    _require_session(task_store, key, session_id)
    v1_body = _tasks.RetryTaskRequest(
        mode=(body.mode if body else None),
        prompt=(Prompt(text=body.prompt) if body and body.prompt is not None else None),
        on_busy=(body.on_busy if body else "queue"),
    )
    result = _tasks.retry_task(
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
        "run": (run_view(result["run"]) if result.get("run") else None),
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
async def stream_session_events(
    request: Request,
    session_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
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
    """
    from control.app import DisconnectAwareStreamingResponse

    record = _require_session(task_store, key, session_id)
    agent_id = record.agent_id
    try:
        last_id = int(last_event_id) if last_event_id else 0
    except ValueError:
        last_id = 0
    start_line = max(1, last_id + 1)

    backend = getattr(plane, "backend", None)
    keepalive_s: float = getattr(request.app.state, "keepalive_s", 15.0)

    def _status_bits() -> tuple[str, str, str]:
        """Current (status, phase, frame) — polled so transitions stream live."""
        fresh = task_store.get(session_id) or record
        ws = _tasks._ws_record(plane, fresh.agent_id)
        status, reason = _tasks._aggregate_status(fresh, run_states, plane, ws)
        st, ph = map_session_status(status, reason)
        payload = {"type": "session.status", "status": st, "phase": ph}
        return st, ph, f"event: session.status\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _status_frame() -> str:
        return _status_bits()[2]

    def _live_handle() -> tuple[Any, Any]:
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

    def _frame(raw: str, lineno: int, current_turn: int) -> tuple[str | None, int]:
        """Normalize one events.jsonl line into an SSE frame (or None)."""
        obj = _routes._parse_event_line(raw)
        current_turn = _events.track_turn(obj, current_turn)
        norm = _events.normalize(obj, current_turn)
        if norm is not None and lineno >= start_line:
            return format_sse(lineno, norm), current_turn
        return None, current_turn

    async def gen() -> AsyncIterator[str]:
        nonlocal agent_id
        proc: Any = None
        current_turn = 0
        last_status: tuple[str, str] | None = None
        next_status = 0.0

        def _status_tick() -> str | None:
            """A session.status frame only when the wire state changed."""
            nonlocal last_status
            st, ph, frame = _status_bits()
            if (st, ph) == last_status:
                return None
            last_status = (st, ph)
            return frame

        try:
            yield ": keepalive\n\n"
            st, ph, opening = _status_bits()
            last_status = (st, ph)
            yield opening
            handle, poll = _live_handle()
            next_ka = time.monotonic() + keepalive_s
            # Wait out the provisioning window: the sandbox appears once the
            # background worker binds it; a terminal/gone session exits to
            # the replay path below. SOR-265: the record may still be unbound
            # at connect time — re-read it until an agent lands.
            while handle is None or poll is None or not poll.alive:
                if agent_id is None:
                    fresh = task_store.get(session_id) or record
                    if fresh.status in _tasks._TASK_TERMINAL:
                        break
                    agent_id = fresh.agent_id
                    if agent_id is None:
                        now = time.monotonic()
                        if now >= next_ka:
                            yield ": keepalive\n\n"
                            next_ka = now + keepalive_s
                        await asyncio.sleep(0.05)
                        handle, poll = _live_handle()
                        continue
                rec_now = plane.get(agent_id)
                if rec_now is None or rec_now.status in ("closed", "timed_out", "lost"):
                    break
                statuses = _tasks._run_statuses_for(agent_id, run_states, plane)
                if statuses and all(s in RUN_TERMINAL for _, s in statuses):
                    break
                now = time.monotonic()
                if now >= next_status:
                    next_status = now + _STATUS_POLL_S
                    tick = _status_tick()
                    if tick is not None:
                        yield tick
                if now >= next_ka:
                    yield ": keepalive\n\n"
                    next_ka = now + keepalive_s
                await asyncio.sleep(0.05)
                handle, poll = _live_handle()
            if backend is not None and handle is not None and poll is not None and poll.alive:
                proc = await asyncio.to_thread(
                    backend.exec,
                    handle,
                    ["tail", "-n", "+1", "-F", str(handle.root / "events.jsonl")],
                    sandbox_env(handle),
                )
                line_q: queue.Queue[tuple[str, str | None]] = queue.Queue()

                def _reader() -> None:
                    try:
                        for line in proc.stdout:
                            line_q.put(("line", line))
                    except Exception:
                        pass
                    finally:
                        line_q.put(("eof", None))

                threading.Thread(target=_reader, daemon=True, name="sbx-v2-sse-tail").start()

                lineno = 0
                next_ka = time.monotonic() + keepalive_s
                while True:
                    try:
                        kind, payload = line_q.get_nowait()
                    except queue.Empty:
                        now = time.monotonic()
                        if now >= next_status:
                            next_status = now + _STATUS_POLL_S
                            tick = _status_tick()
                            if tick is not None:
                                yield tick
                        if now >= next_ka:
                            yield ": keepalive\n\n"
                            next_ka = now + keepalive_s
                        await asyncio.sleep(0.05)
                        continue
                    if kind == "eof":
                        break
                    raw = payload or ""
                    # Contract: id is the events.jsonl 1-based line number —
                    # a blank/torn line still consumes one.
                    lineno += 1
                    if not raw.strip():
                        continue
                    frame, current_turn = _frame(raw, lineno, current_turn)
                    if frame is not None:
                        yield frame
                    now = time.monotonic()
                    if now >= next_ka:
                        yield ": keepalive\n\n"
                        next_ka = now + keepalive_s
                yield _status_frame()
                return

            # Sandbox unreachable: replay the file if locally readable, then
            # keep the stream open like /api/* and /v1 do.
            lines: list[str] = []
            if backend is not None and handle is not None:
                try:
                    text = read_text(backend, handle, "events.jsonl")
                except Exception:
                    text = None
                if text:
                    lines = text.splitlines()
            lineno = 0
            for raw in lines:
                lineno += 1
                if not raw.strip():
                    continue
                frame, current_turn = _frame(raw, lineno, current_turn)
                if frame is not None:
                    yield frame
            if not lines:
                # Sandbox gone: stitch the durable per-run transcripts.
                activity = getattr(request.app.state, "run_activity", None)
                ns: set[int] = set()
                rec_now = plane.get(agent_id) if agent_id else None
                if rec_now is not None:
                    ns = _routes._known_run_ns(rec_now, _routes._ledger(plane), run_states)
                elif agent_id is not None:
                    ledger = _routes._ledger(plane)
                    if ledger is not None:
                        try:
                            ns = {r.n for r in ledger.list(agent_id)}
                        except Exception:
                            ns = set()
                entries: list[dict[str, Any]] = []
                for n in sorted(ns):
                    try:
                        entries.extend(activity.get(agent_id, n) if activity is not None else [])
                    except Exception:
                        pass
                entries.sort(key=lambda e: e["id"])
                for entry in entries:
                    if entry["id"] < start_line:
                        continue
                    obj = entry["event"]
                    current_turn = _events.track_turn(obj, current_turn)
                    norm = _events.normalize(obj, current_turn)
                    if norm is not None:
                        yield format_sse(entry["id"], norm)
            tick = _status_tick()
            yield tick if tick is not None else _status_frame()
            while True:
                now = time.monotonic()
                if now >= next_status:
                    next_status = now + _STATUS_POLL_S
                    tick = _status_tick()
                    if tick is not None:
                        yield tick
                if now >= next_ka:
                    yield ": keepalive\n\n"
                    next_ka = now + keepalive_s
                await asyncio.sleep(0.05)
        finally:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass

    return DisconnectAwareStreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
