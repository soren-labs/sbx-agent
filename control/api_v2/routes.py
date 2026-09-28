"""SOR-256: Session-first ``/v2`` routes — a typed facade over the task engine.

A *Session* is the V2 product object; internally it is the durable
:class:`~control.tasks.TaskRecord` whose ``agent_id``/``run_id`` wire into the
existing execution path. Nothing in a V2 response exposes Task/Agent/Run/Revision
ids to the caller.

Write semantics: every mutating endpoint persists durable intent and ACKs
without waiting on Modal/provider/GitHub work. Resolution, dispatch and
delivery happen on daemon threads; a bounded wait (``ack_budget``) lets fast
paths still answer with the applied result — clients always learn outcomes via
``GET /v2/sessions/{id}/events`` or a detail re-read, never by request timeout.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from typing import Any

from fastapi import Depends, Header, Request
from pydantic import BaseModel

from control import tasks as taskmod
from control.api_v1 import routes as _routes
from control.api_v1 import tasks as _tasks
from control.api_v1.deps import (
    agents_key,
    get_capabilities,
    get_plane,
    get_registry,
    get_repo_resolver,
    get_resources,
    get_run_reporter,
    get_run_states,
    get_scheduler,
    get_task_store,
    get_v1_state,
    get_workflow_service,
)
from control.api_v1.errors import V1ApiError, not_found
from control.api_v1.lifecycle import RunStateStore, request_fingerprint
from control.api_v1.state import V1State
from control.api_v1.tasks import CreateTaskRequest
from control.api_v2 import projection as proj
from control.api_v2 import router
from control.api_v2.deps import get_ack_budget, get_v2_hub
from control.api_v2.events import SessionEventsHub
from control.api_v2.schemas import (
    CreateMessageRequest,
    CreateSessionRequest,
    DeliverSessionRequest,
    RetrySessionRequest,
    SessionChangesResponse,
    SessionDeliverResponse,
    SessionDetailResponse,
    SessionListResponse,
    SessionMessageResponse,
    SessionResponse,
    SessionSummaryView,
)
from control.ports import AccountRegistry, ApiKey, Scheduler
from control.revisions import RevisionError, RevisionService
from control.service import SessionConflict
from control.tasks import TaskRecord, TaskRefusal, TaskStore

_LIST_PAGE = 100
_MESSAGE_LIMIT = 50
_ACTIVITY_LIMIT = 50


# ---------------------------------------------------------------------------
# request mapping
# ---------------------------------------------------------------------------


def _delivery_spec(delivery: Any) -> dict[str, Any] | None:
    """V2 ``delivery {mode,target,draft,...}`` → V1 ``delivery`` block."""
    if delivery is None:
        return None
    mode = delivery.mode
    if mode == "none":
        return None
    if mode == "branch":
        return {"branch": delivery.target}
    pr: dict[str, Any] = {"draft": bool(delivery.draft)}
    if delivery.target:
        pr["target"] = delivery.target
    if delivery.title:
        pr["title"] = delivery.title
    if delivery.body:
        pr["body"] = delivery.body
    out: dict[str, Any] = {"pull_request": pr}
    if delivery.branch:
        out["branch"] = delivery.branch
    if mode == "auto":
        out["auto_publish"] = True
    return out


def _task_spec(body: CreateSessionRequest) -> dict[str, Any]:
    """The V1 task-declaration spec the shared resolver consumes."""
    spec: dict[str, Any] = {"prompt": {"text": body.prompt}}
    if body.title:
        spec["name"] = body.title
    if body.repository is not None:
        spec["source"] = {"repo": body.repository.repo, "ref": body.repository.ref}
    if body.execution is not None:
        spec["execution"] = body.execution.model_dump(exclude_none=True)
    delivery = _delivery_spec(body.delivery)
    if delivery is not None:
        spec["delivery"] = delivery
    for key in ("metadata", "output_contract", "resources", "compute", "idle_timeout_s"):
        value = getattr(body, key)
        if value is None:
            continue
        spec[key] = value.model_dump(exclude_none=True) if isinstance(value, BaseModel) else value
    return spec


def _v1_body(spec: dict[str, Any]) -> CreateTaskRequest:
    """Re-validate the mapped spec as the V1 model (defense in depth)."""
    return CreateTaskRequest.model_validate(spec)


# ---------------------------------------------------------------------------
# session lookup / projection helpers
# ---------------------------------------------------------------------------


def _require_session(task_store: TaskStore, key: ApiKey, session_id: str) -> TaskRecord:
    record = task_store.get(session_id)
    if record is None or record.owner != key.id:
        raise not_found("session not found")
    return record


def _require_session_agent(record: TaskRecord, plane: Any) -> Any:
    if record.agent_id is None:
        raise V1ApiError(409, "session_not_runnable", "session has no agent yet")
    rec = plane.get(record.agent_id)
    if rec is None:
        raise V1ApiError(409, "session_not_runnable", "session's agent is gone")
    return rec


def _settled_summary(
    record: TaskRecord,
    *,
    plane: Any,
    v1: V1State,
    run_states: RunStateStore,
    task_store: TaskStore,
) -> SessionSummaryView:
    status, ws = proj.settle(record, task_store, plane, run_states)
    return SessionSummaryView(
        **proj.summary_view(
            record, plane=plane, v1=v1, run_states=run_states, ws=ws, aggregate=status
        )
    )


def _conflict(exc: SessionConflict, plane: Any) -> V1ApiError:
    return V1ApiError(
        exc.code,
        exc.error,
        exc.error,
        retry_after=(plane.turn_max_seconds if exc.error == "turn_in_progress" else None),
    )


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def _fail_session(record: TaskRecord, task_store: TaskStore, exc: Exception) -> None:
    """Persist a terminal resolution failure — the session exists and is done."""
    code = getattr(exc, "code", None) or getattr(exc, "error", None) or "internal"
    retryable = False
    if isinstance(exc, (V1ApiError, TaskRefusal)):
        retryable = bool(getattr(exc, "retryable", False))
    elif not isinstance(exc, Exception):
        retryable = False
    else:
        retryable = True
    try:
        latest = task_store.get(record.id) or record
    except Exception:
        latest = record
    latest.transitions.append(
        {
            "status": "failed",
            "reason": str(code),
            "detail": str(exc)[:500],
            "retryable": retryable,
            "at": taskmod._iso_now(),
        }
    )
    latest.status = "failed"
    latest.updated_at = taskmod._iso_now()
    try:
        task_store.put(latest)
    except Exception:
        pass


def _dispatch_session(
    record: TaskRecord,
    spec: dict[str, Any],
    key: ApiKey,
    *,
    plane: Any,
    registry: AccountRegistry,
    scheduler: Scheduler,
    v1: V1State,
    run_states: RunStateStore,
    workflows: Any,
    reporter: Any,
    resources_registry: Any,
    capabilities: Any,
    task_store: TaskStore,
    resolver: Any,
    idempotency_key: str | None,
    idempotency_fingerprint: str | None,
    owned: Any = None,
) -> None:
    """Worker: resolve → reserve → bind the durable record → launch run-1.

    The Task record is already durable when this runs, so the synchronous
    ACK never waits on the resolver's repo probes or the scheduler.
    """
    on_provisioned = None
    if owned is not None and idempotency_key:
        on_provisioned = lambda: v1.idempotency.settle(  # noqa: E731
            key.id, idempotency_key, owned
        )
    try:
        body = _v1_body(spec)
        resolution = _tasks._resolve(
            body,
            registry=registry,
            scheduler=scheduler,
            capabilities=capabilities,
            resolver=resolver,
        )
        git = resolution.git
        workspace = resolution.source.workspace() if resolution.source is not None else None
        contract = _routes._normalize_contract(body.output_contract)
        compute = _routes._validate_compute(body)
        if contract is not None and _routes._ledger(plane) is None:
            raise V1ApiError(409, "session_not_runnable", "output contracts require the run ledger")
        eligible = [c for c in resolution.execution.candidates if c.eligible]
        if not eligible:
            raise V1ApiError(409, "account_unavailable", "no eligible account")
        pick = resolution.execution.pick
        rest = sorted(
            (c for c in eligible if c is not pick),
            key=lambda c: (c.last_used_at or "", c.account_id),
        )
        candidates = ([pick] if pick is not None else []) + rest
        last_error: V1ApiError | None = None
        for candidate in candidates:
            agent = _tasks._candidate_spec(candidate)
            request = _tasks._agent_request(body, agent)
            resources = _routes._validate_resources(request, resources_registry)
            try:
                result = _routes._create_agent_once(
                    request,
                    key,
                    plane,
                    registry,
                    scheduler,
                    v1,
                    run_states,
                    workflows,
                    capabilities=capabilities,
                    reporter=reporter,
                    idempotency_key=idempotency_key,
                    idempotency_fingerprint=idempotency_fingerprint,
                    workspace=workspace,
                    git=git,
                    output_contract=contract,
                    resources=resources,
                    compute=compute,
                    reasoning_effort=candidate.effort,
                    on_provisioned=on_provisioned,
                )
            except V1ApiError as exc:
                if exc.code in _tasks._RETRYABLE_SCHEDULE_CODES:
                    last_error = exc
                    continue
                raise
            latest = task_store.get(record.id) or record
            latest.resolved = _tasks._resolved_for(resolution, candidate)
            latest.agent_id = result["agent"]["id"]
            latest.run_id = result["run"]["id"]
            latest.updated_at = taskmod._iso_now()
            # The V2 replay body — a replayed Idempotency-Key returns this.
            latest.response = {
                "session": _settled_summary(
                    latest,
                    plane=plane,
                    v1=v1,
                    run_states=run_states,
                    task_store=task_store,
                ).model_dump(mode="json")
            }
            task_store.put(latest)
            if owned is not None and idempotency_key:
                v1.idempotency.complete(
                    key.id, idempotency_key, owned, agent_id=latest.agent_id, body=latest.response
                )
                v1.idempotency.settle(key.id, idempotency_key, owned)
            return
        assert last_error is not None
        raise last_error
    except Exception as exc:
        _fail_session(record, task_store, exc)
        if owned is not None and idempotency_key:
            try:
                # A failed create still pins the key — a replay must resolve
                # to this session's (failed) state, not spawn a duplicate.
                fresh = task_store.get(record.id) or record
                body_out = {
                    "session": _settled_summary(
                        fresh,
                        plane=plane,
                        v1=v1,
                        run_states=run_states,
                        task_store=task_store,
                    ).model_dump(mode="json")
                }
                v1.idempotency.complete(
                    key.id, idempotency_key, owned, agent_id=None, body=body_out
                )
                v1.idempotency.settle(key.id, idempotency_key, owned)
            except Exception:
                pass


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
) -> SessionResponse:
    """Create a session: durable record now, resolve+dispatch off-path.

    The ACK is O(ms): the record persists synchronously (status ``queued`` /
    phase ``resolving``) and the resolution → account reserve → agent create
    → run-1 dispatch chain runs on a worker thread. ``Idempotency-Key``
    replays the original session after restart; a different body under a used
    key is a 409 ``idempotency_conflict``.
    """
    spec = _task_spec(body)
    fingerprint = request_fingerprint(body)
    owned = None
    if idempotency_key:
        outcome, entry = v1.idempotency.claim(key.id, idempotency_key, fingerprint)
        if outcome == "hit":
            return SessionResponse(**entry.body)
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
        prior = task_store.find_by_idempotency(key.id, idempotency_key)
        if prior is not None:
            prior_fp = (prior.idempotency or {}).get("fingerprint")
            if prior_fp not in (None, fingerprint):
                v1.idempotency.abandon(key.id, idempotency_key, entry)
                raise V1ApiError(
                    409,
                    "idempotency_conflict",
                    "Idempotency-Key was already used with a different request body",
                )
            body_out = prior.response or {
                "session": _settled_summary(
                    prior,
                    plane=plane,
                    v1=v1,
                    run_states=run_states,
                    task_store=task_store,
                ).model_dump(mode="json")
            }
            v1.idempotency.complete(
                key.id, idempotency_key, entry, agent_id=prior.agent_id, body=body_out
            )
            v1.idempotency.settle(key.id, idempotency_key, entry)
            return SessionResponse(**body_out)
        owned = entry

    record = TaskRecord(
        id=taskmod.new_task_id(),
        owner=key.id,
        status="queued",
        request=spec,
        resolved=None,
        agent_id=None,
        run_id=None,
        created_at=taskmod._iso_now(),
        updated_at=taskmod._iso_now(),
        idempotency=(
            {"key_id": key.id, "key": idempotency_key, "fingerprint": fingerprint}
            if idempotency_key
            else None
        ),
    )
    task_store.put(record)
    threading.Thread(
        target=_dispatch_session,
        args=(record, spec, key),
        kwargs={
            "plane": plane,
            "registry": registry,
            "scheduler": scheduler,
            "v1": v1,
            "run_states": run_states,
            "workflows": workflows,
            "reporter": reporter,
            "resources_registry": resources_registry,
            "capabilities": capabilities,
            "task_store": task_store,
            "resolver": resolver,
            "idempotency_key": idempotency_key,
            "idempotency_fingerprint": fingerprint,
            "owned": owned,
        },
        daemon=True,
        name=f"sbx-v2-dispatch-{record.id}",
    ).start()
    return SessionResponse(
        session=_settled_summary(
            record,
            plane=plane,
            v1=v1,
            run_states=run_states,
            task_store=task_store,
        )
    )


# ---------------------------------------------------------------------------
# list / detail
# ---------------------------------------------------------------------------


@router.get("/sessions", response_model=SessionListResponse)
def list_sessions(
    key: ApiKey = Depends(agents_key),
    task_store: TaskStore = Depends(get_task_store),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    cursor: str | None = None,
    limit: int = _LIST_PAGE,
) -> SessionListResponse:
    """The caller's sessions, created_at order, cursor-paginated."""
    records = task_store.list(key.id)
    try:
        offset = int(cursor) if cursor else 0
    except ValueError:
        offset = 0
    limit = max(1, min(limit, _LIST_PAGE))
    page = records[offset : offset + limit]
    next_cursor = str(offset + limit) if offset + limit < len(records) else None
    sessions = [
        _settled_summary(record, plane=plane, v1=v1, run_states=run_states, task_store=task_store)
        for record in page
    ]
    return SessionListResponse(sessions=sessions, next_cursor=next_cursor)


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
def get_session(
    session_id: str,
    request: Request,
    key: ApiKey = Depends(agents_key),
    task_store: TaskStore = Depends(get_task_store),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
) -> SessionDetailResponse:
    """Bounded first-view projection — point reads only, no sandbox exec."""
    record = _require_session(task_store, key, session_id)
    status, ws = proj.settle(record, task_store, plane, run_states)
    detail = proj.detail_view(
        record,
        plane=plane,
        v1=v1,
        run_states=run_states,
        revisions=getattr(request.app.state, "revisions", None),
        ws=ws,
        aggregate=status,
        message_limit=_MESSAGE_LIMIT,
        activity_limit=_ACTIVITY_LIMIT,
    )
    return SessionDetailResponse(session=detail)


# ---------------------------------------------------------------------------
# follow-up message
# ---------------------------------------------------------------------------


@router.post("/sessions/{session_id}/messages", status_code=202)
def post_message(
    session_id: str,
    body: CreateMessageRequest,
    request: Request,
    key: ApiKey = Depends(agents_key),
    task_store: TaskStore = Depends(get_task_store),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: Any = Depends(get_run_reporter),
    workflows: Any = Depends(get_workflow_service),
    hub: SessionEventsHub = Depends(get_v2_hub),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> SessionMessageResponse:
    """Post a follow-up message — queued durably, dispatched off-path.

    ``queue=True`` semantics: the message parks as a durable QUEUED run when
    the session is busy; an idle session dispatches it immediately. Either
    way the response carries ``accepted`` — ``message`` is populated when the
    durable write landed inside the ACK budget, else ``None`` (subscribe to
    events for ``message.created``).
    """
    record = _require_session(task_store, key, session_id)
    rec = _require_session_agent(record, plane)
    if rec.status in ("closed", "timed_out", "lost") or record.status == "cancelled":
        raise V1ApiError(409, "session_not_runnable", "session is not runnable")
    contract = _routes._normalize_contract(body.output_contract)
    if contract is not None and _routes._ledger(plane) is None:
        raise V1ApiError(409, "session_not_runnable", "output contracts require the run ledger")
    fingerprint = request_fingerprint(body)
    pin_key = f"session:{session_id}:msg:{idempotency_key}" if idempotency_key else None
    owned = None
    pin = None
    if idempotency_key:
        outcome, entry = v1.idempotency.claim(key.id, pin_key or "", fingerprint)
        if outcome == "hit":
            return SessionMessageResponse(**entry.body)
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
                "a message with this Idempotency-Key is still in progress",
            )
        ledger = _routes._ledger(plane)
        if ledger is not None:
            prior = ledger.find_by_idempotency(record.agent_id, key.id, pin_key or "")
            if prior is not None:
                prior_fp = (prior.idempotency or {}).get("fingerprint")
                if prior_fp not in (None, fingerprint):
                    v1.idempotency.abandon(key.id, pin_key or "", entry)
                    raise V1ApiError(
                        409,
                        "idempotency_conflict",
                        "Idempotency-Key was already used with a different request body",
                    )
                body_out = {
                    "session": _settled_summary(
                        record,
                        plane=plane,
                        v1=v1,
                        run_states=run_states,
                        task_store=task_store,
                    ).model_dump(mode="json"),
                    "accepted": True,
                    "message": _message_for_turn(rec, f"turn-{prior.n}"),
                }
                v1.idempotency.complete(
                    key.id, pin_key or "", entry, agent_id=record.agent_id, body=body_out
                )
                v1.idempotency.settle(key.id, pin_key or "", entry)
                return SessionMessageResponse(**body_out)
        owned = entry
        pin = {"key_id": key.id, "key": pin_key, "fingerprint": fingerprint}

    def work() -> dict[str, Any]:
        turn_id = plane.post_message(
            record.agent_id,
            body.prompt,
            output_contract=contract,
            queue=True,
            idempotency=pin,
        )
        if body.metadata is not None:
            workflows.attach(owner=key.id, agent_id=record.agent_id, metadata=body.metadata)
        rec_now = plane.get(record.agent_id)
        message = _message_for_turn(rec_now, turn_id) if rec_now is not None else None
        return {
            "session": _settled_summary(
                task_store.get(session_id) or record,
                plane=plane,
                v1=v1,
                run_states=run_states,
                task_store=task_store,
            ).model_dump(mode="json"),
            "accepted": True,
            "message": message,
        }

    def side_effects(box: dict[str, Any]) -> None:
        if owned is None:
            return
        if "result" in box:
            v1.idempotency.complete(
                key.id, pin_key or "", owned, agent_id=record.agent_id, body=box["result"]
            )
            v1.idempotency.settle(key.id, pin_key or "", owned)
        elif "error" in box:
            v1.idempotency.abandon(key.id, pin_key or "", owned)

    done = threading.Event()
    shared_box: dict[str, Any] = {}

    def runner() -> None:
        try:
            shared_box["result"] = work()
        except Exception as exc:
            shared_box["error"] = exc
            # Deferred refusals surface in-band for subscribed clients.
            hub.emit_for(
                session_id,
                "activity.completed",
                {
                    "activity": {
                        "id": "msg-refused",
                        "kind": "error",
                        "status": "failed",
                        "message": str(exc),
                    }
                },
            )
        side_effects(shared_box)
        done.set()

    threading.Thread(target=runner, daemon=True, name=f"sbx-v2-msg-{session_id}").start()
    if done.wait(get_ack_budget(request)):
        if "error" in shared_box:
            raise _mutation_error(shared_box["error"], plane)
        return SessionMessageResponse(**shared_box["result"])
    # Deferred: the worker finishes the durable enqueue off-path; a refusal
    # surfaces as an in-band activity error for subscribed clients.
    return SessionMessageResponse(
        session=_settled_summary(
            record, plane=plane, v1=v1, run_states=run_states, task_store=task_store
        ),
        accepted=True,
        message=None,
    )


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


@router.post("/sessions/{session_id}/cancel", response_model=SessionResponse)
def cancel_session(
    session_id: str,
    request: Request,
    key: ApiKey = Depends(agents_key),
    task_store: TaskStore = Depends(get_task_store),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
) -> SessionResponse:
    """Cancel the session's outstanding work — idempotent.

    The durable part lands synchronously (queued runs cancelled, a parked
    first turn dropped); the sandbox stop itself runs off-path so the ACK
    never waits on provider teardown.
    """
    record = _require_session(task_store, key, session_id)
    status, _ws = proj.settle(record, task_store, plane, run_states)
    if status == "cancelled" or record.agent_id is None:
        if status != "cancelled":
            record.transitions.append(
                {"status": "cancelled", "reason": "session_cancelled", "at": taskmod._iso_now()}
            )
            record.status = "cancelled"
            record.updated_at = taskmod._iso_now()
            task_store.put(record)
        return SessionResponse(
            session=_settled_summary(
                record, plane=plane, v1=v1, run_states=run_states, task_store=task_store
            )
        )
    if status in _tasks._TASK_TERMINAL:
        return SessionResponse(
            session=_settled_summary(
                record, plane=plane, v1=v1, run_states=run_states, task_store=task_store
            )
        )
    agent_id = record.agent_id
    ledger = _routes._ledger(plane)
    queued = [n for n, s in _tasks._run_statuses_for(agent_id, run_states, plane) if s == "QUEUED"]
    for n in queued:
        if ledger is not None:
            ledger.cancel(agent_id, n)
        else:
            run_states.transition(agent_id, n, "CANCELLED")
        v1.mark_cancelled(agent_id, n)
    rec = plane.get(agent_id)
    if rec is not None and rec.status == "creating":
        plane.discard_queued_first_turn(agent_id)
        run_states.transition(agent_id, 1, "CANCELLED")
        v1.mark_cancelled(agent_id, 1)
        rec = plane.get(agent_id) or rec

    def work() -> None:
        latest = plane.get(agent_id)
        if latest is not None and latest.status == "running":
            try:
                plane.stop(agent_id)
            except KeyError:
                pass
        fresh = task_store.get(session_id) or record
        if fresh.status != "cancelled":
            fresh.transitions.append(
                {
                    "status": "cancelled",
                    "reason": "session_cancelled",
                    "at": taskmod._iso_now(),
                }
            )
            fresh.status = "cancelled"
            fresh.updated_at = taskmod._iso_now()
            try:
                task_store.put(fresh)
            except Exception:
                pass

    completed, _box = proj.run_with_budget(work, get_ack_budget(request))
    if not completed:
        # Stop still in flight — the durable intent is already written; the
        # settle below reports the cancelled aggregate immediately after.
        pass
    record = task_store.get(session_id) or record
    if record.status != "cancelled":
        record.transitions.append(
            {"status": "cancelled", "reason": "session_cancelled", "at": taskmod._iso_now()}
        )
        record.status = "cancelled"
        record.updated_at = taskmod._iso_now()
        task_store.put(record)
    return SessionResponse(
        session=_settled_summary(
            record, plane=plane, v1=v1, run_states=run_states, task_store=task_store
        )
    )


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------


@router.post("/sessions/{session_id}/retry", status_code=202)
def retry_session(
    session_id: str,
    body: RetrySessionRequest | None = None,
    *,
    request: Request,
    key: ApiKey = Depends(agents_key),
    task_store: TaskStore = Depends(get_task_store),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
) -> SessionMessageResponse:
    """Retry the session's failed step on the same agent.

    ``mode=None`` picks by state: ``delivery_failed`` re-executes the publish;
    a terminal run verdict re-runs the prompt (or the override). An active
    session is a ``task_active`` 409 carrying ``retry_after``.
    """
    record = _require_session(task_store, key, session_id)
    body = body or RetrySessionRequest()
    status, ws = proj.settle(record, task_store, plane, run_states)
    mode = body.mode
    if mode is None:
        mode = "delivery" if status == "delivery_failed" else "run"
    if mode == "delivery":
        delivery = proj.delivery_view(record, ws, _revisions(request, record))
        if delivery is None:
            raise V1ApiError(409, "task_not_retryable", "session has no delivery policy")
        _require_session_agent(record, plane)
        if delivery["status"] == "delivered":
            return SessionMessageResponse(
                session=_settled_summary(
                    record, plane=plane, v1=v1, run_states=run_states, task_store=task_store
                ),
                accepted=True,
                message=None,
            )

        def work() -> None:
            _tasks._publish_delivery(plane, record.agent_id)

    else:
        if status in ("running", "queued", "delivering"):
            raise V1ApiError(
                409,
                "task_active",
                "session still has active work",
                retry_after=getattr(plane, "turn_max_seconds", None),
            )
        _require_session_agent(record, plane)
        text = body.prompt or ((record.request or {}).get("prompt") or {}).get("text")
        if not text:
            raise V1ApiError(409, "task_not_retryable", "session has no prompt to retry")

        def work() -> None:  # noqa: F811
            plane.post_message(record.agent_id, str(text), queue=True)

    done, box = proj.run_with_budget(work, get_ack_budget(request))
    if done and "error" in box:
        raise _mutation_error(box["error"], plane)
    record = task_store.get(session_id) or record
    return SessionMessageResponse(
        session=_settled_summary(
            record, plane=plane, v1=v1, run_states=run_states, task_store=task_store
        ),
        accepted=True,
        message=None,
    )


# ---------------------------------------------------------------------------
# events (SSE)
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/events")
async def session_events(
    request: Request,
    session_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    key: ApiKey = Depends(agents_key),
    task_store: TaskStore = Depends(get_task_store),
    hub: SessionEventsHub = Depends(get_v2_hub),
) -> Any:
    """Normalized event stream — one tail + one poller per session.

    ``Last-Event-ID`` resumes from the feed's bounded replay buffer; a stale
    (post-restart) id replays the retained window. Multiple concurrent
    subscribers share the single tail/poller pair — remote polling never
    multiplies.
    """
    from control.app import DisconnectAwareStreamingResponse

    _require_session(task_store, key, session_id)
    feed = hub.feed(session_id)
    keepalive_s: float = getattr(request.app.state, "keepalive_s", 15.0)

    async def gen() -> Any:
        try:
            last_seq = int(last_event_id) if last_event_id else None
        except ValueError:
            last_seq = None
        if last_seq is not None and last_seq > feed.seq:
            last_seq = None
        q = feed.subscribe(last_seq)
        yield ": keepalive\n\n"
        try:
            for event in feed.replay(last_seq):
                yield event.sse()
            next_ka = time.monotonic() + keepalive_s
            while feed.live:
                try:
                    event = q.get_nowait()
                except queue.Empty:
                    now = time.monotonic()
                    if now >= next_ka:
                        yield ": keepalive\n\n"
                        next_ka = now + keepalive_s
                    await asyncio.sleep(0.05)
                    continue
                yield event.sse()
        finally:
            feed.unsubscribe(q)

    return DisconnectAwareStreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# changes / deliver
# ---------------------------------------------------------------------------


def _revisions(request: Request, record: TaskRecord) -> list[Any]:
    """Durable revisions for the session's agent (empty when none)."""
    revisions: Any = getattr(request.app.state, "revisions", None)
    if revisions is None or record.agent_id is None:
        return []
    try:
        return list(revisions.list(record.agent_id))
    except Exception:
        return []


@router.get("/sessions/{session_id}/changes", response_model=SessionChangesResponse)
def session_changes(
    session_id: str,
    request: Request,
    key: ApiKey = Depends(agents_key),
    task_store: TaskStore = Depends(get_task_store),
    plane: Any = Depends(get_plane),
    run_states: RunStateStore = Depends(get_run_states),
) -> SessionChangesResponse:
    """Session-level changes — durable revisions; Revision ids stay internal."""
    record = _require_session(task_store, key, session_id)
    proj.settle(record, task_store, plane, run_states)
    ws = _tasks._ws_record(plane, record.agent_id)
    return SessionChangesResponse(changes=proj.changes_view(_revisions(request, record), ws))


@router.post("/sessions/{session_id}/deliver", status_code=202)
def deliver_session(
    session_id: str,
    request: Request,
    body: DeliverSessionRequest | None = None,
    *,
    key: ApiKey = Depends(agents_key),
    task_store: TaskStore = Depends(get_task_store),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
) -> SessionDeliverResponse:
    """Deliver the session's work: push the durable payload + open/update the PR.

    The deliver leg runs host-side on the durable artifact — it works after
    the sandbox is gone. GitHub push/PR latency never blocks the ACK; watch
    ``delivery.updated`` for the outcome.
    """
    record = _require_session(task_store, key, session_id)
    revisions_service: RevisionService | None = getattr(request.app.state, "revisions", None)
    if revisions_service is None:
        raise V1ApiError(409, "workspace_unavailable", "revision service unavailable")
    if record.agent_id is None:
        raise V1ApiError(404, "revision_not_found", "session has no work to deliver yet")
    rows = _revisions(request, record)
    if not rows:
        raise V1ApiError(404, "revision_not_found", "session has no work to deliver yet")
    revision = rows[-1]
    overrides: dict[str, Any] = {}
    if body is not None:
        if body.branch:
            overrides["branch"] = body.branch
        pr: dict[str, Any] = {}
        if body.target:
            pr["target"] = body.target
        if body.draft is not None:
            pr["draft"] = body.draft
        if body.title:
            pr["title"] = body.title
        if body.body:
            pr["body"] = body.body
        if pr:
            overrides["pull_request"] = pr

    def work() -> None:
        revisions_service.deliver(revision, overrides=overrides or None)

    done, box = proj.run_with_budget(work, get_ack_budget(request))
    if done and "error" in box:
        exc = box["error"]
        if isinstance(exc, RevisionError):
            raise V1ApiError(exc.status_code, exc.code, exc.message)
        raise _mutation_error(exc, plane)
    record = task_store.get(session_id) or record
    proj.settle(record, task_store, plane, run_states)
    ws = _tasks._ws_record(plane, record.agent_id)
    delivery = proj.delivery_view(record, ws, _revisions(request, record))
    return SessionDeliverResponse(
        session=_settled_summary(
            record, plane=plane, v1=v1, run_states=run_states, task_store=task_store
        ),
        delivery=delivery,
        accepted=True,
    )


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


def _message_for_turn(rec: Any, turn_id: str) -> dict[str, Any] | None:
    """The public view of the user message bound to ``turn_id``."""
    for i, message in enumerate(rec.messages or [], start=1):
        if message.get("turn_id") == turn_id and message.get("role") == "user":
            return proj._message_view(i, message)
    return None


def _mutation_error(exc: Exception, plane: Any) -> V1ApiError:
    """Translate a worker-surfaced refusal to the canonical error body."""
    if isinstance(exc, V1ApiError):
        return exc
    if isinstance(exc, SessionConflict):
        return _conflict(exc, plane)
    if isinstance(exc, KeyError):
        return not_found("agent not found")
    if isinstance(exc, TaskRefusal):
        return V1ApiError(exc.status_code, exc.code, exc.message, retry_after=exc.retry_after)
    return V1ApiError(500, "internal", f"mutation failed: {type(exc).__name__}")
