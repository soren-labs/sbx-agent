"""Session / turn / event / changeset / delivery / delegation routes.

Every read goes to committed projections; live filesystem reads go through
the session's active lease via the runtime pool and degrade to
``executor_unavailable`` when no lease is attached. Mutations are
Idempotency-Key'd and owner-scoped.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, Request
from protocol.runtime import OperationEnvelope, OperationKind

from control.api import views
from control.api.deps import (
    idem_record,
    idem_replay,
    pagination,
    principal_dep,
    require_idem,
    require_workspace,
)
from control.api.errors import ApiError, unsupported
from control.application.sessions import enqueue_job
from control.domain.ids import new_id
from control.domain.jobs import JobKind, TargetFamily
from control.persistence.unit_of_work import SqlUnitOfWork

router = APIRouter(prefix="/api")


def _nf(what: str) -> ApiError:
    return ApiError(404, code="not_found", category="resource", message=f"{what} not found")


def _owner(request: Request, row: dict | None, what: str) -> dict:
    if row is None:
        raise _nf(what)
    if row.get("workspace_id") not in request.state.principal_obj.workspace_ids:
        raise _nf(what)
    return row


def _session(request: Request, uow: SqlUnitOfWork, session_id: str) -> dict:
    for ws in request.state.principal_obj.workspace_ids:
        row = uow.sessions.get(ws, session_id)
        if row is not None:
            return row
    raise _nf("session")


def _harness_ok(body: dict) -> dict:
    h = body.get("harness") or {}
    if h.get("provider_id") != "opencode":
        raise ApiError(
            422,
            code="unsupported_capability",
            category="capability",
            message=f"harness provider {h.get('provider_id')!r} is not available"
            " in this build (supported: opencode)",
        )
    if not h.get("model"):
        raise ApiError(
            422,
            code="validation_failed",
            category="request",
            message="harness.model is required",
        )
    return h


# ---- sessions -------------------------------------------------------------


@router.get("/workspaces/{workspace_id}/sessions")
def list_sessions(
    request: Request,
    workspace_id: str,
    principal: dict = Depends(principal_dep),
    lifecycle: str | None = None,
    role: str | None = None,
    parent_session_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    require_workspace(request, workspace_id)
    lim, _ = pagination(limit=limit, cursor=cursor)
    with SqlUnitOfWork(request.app.state.db) as uow:
        rows = uow.sessions.list(
            workspace_id,
            lifecycle=lifecycle,
            role=role,
            parent_session_id=parent_session_id,
            limit=lim,
        )
    return {"items": [views.session_view(s) for s in rows]}


@router.post("/workspaces/{workspace_id}/sessions", status_code=201)
def create_session(
    request: Request,
    workspace_id: str,
    body: dict,
    principal: dict = Depends(principal_dep),
) -> dict:
    """Create a Session; an embedded ``message`` is accepted in the same
    call and its Message/Turn ids are returned before any provisioning."""
    require_workspace(request, workspace_id)
    require_idem(request)
    harness = _harness_ok(body)
    svc = request.app.state.sessions
    effective_input = body.get("effective_input") or {}
    with SqlUnitOfWork(request.app.state.db) as uow:
        replay = idem_replay(uow, request, workspace_id=workspace_id, body=body)
        if replay is not None:
            return replay["response"]
        digest = hashlib.sha256(repr(sorted(effective_input.items())).encode()).hexdigest()
        session = svc.create_session(
            principal=principal,
            workspace_id=workspace_id,
            role=body.get("role", "author"),
            title=body.get("title"),
            project_version_id=body.get("project_version_id"),
            projectless_spec=body.get("projectless_spec"),
            harness=harness,
            effective_input=effective_input,
            effective_input_digest=body.get("effective_input_digest") or f"sha256:{digest}",
            labels=body.get("labels"),
        )
        session = uow.sessions.get(workspace_id, session["session_id"])
        resp: dict = {"session": views.session_view(session)}
        msg = body.get("message")
        if msg:
            out = svc.post_message(
                principal=principal,
                workspace_id=workspace_id,
                session_id=session["id"],
                author={"kind": "user", "id": principal["id"]},
                routing=msg.get("routing", "queue"),
                content=msg.get("content") or {"text": msg.get("text", "")},
            )
            resp["message_id"] = out["message_id"]
            if out.get("turn_id"):
                resp["turn_id"] = out["turn_id"]
        idem_record(
            uow,
            request,
            workspace_id=workspace_id,
            body=body,
            status=201,
            response=resp,
            resource_ids={"session_id": session["id"]},
        )
        uow.commit()
        return resp


@router.get("/sessions/{session_id}")
def get_session(
    request: Request, session_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        s = _session(request, uow, session_id)
        ws = s["workspace_id"]
        out = {"session": views.session_view(s)}
        out["worktree"] = _safe(
            uow.worktrees.get_by_session(ws, session_id),
            ("id", "state", "generation", "repository", "base_sha"),
        )
        lease = uow.leases.active_by_session(ws, session_id)
        out["executor"] = views.lease_view(lease) if lease else None
        turn = uow.turns.active_by_session(ws, session_id)
        out["active_turn"] = views.turn_view(turn) if turn else None
        out["watermark"] = uow.events.watermark(ws, session_id)
        return out


def _safe(row: dict | None, keys: tuple) -> dict | None:
    if row is None:
        return None
    return {k: row.get(k) for k in keys}


@router.patch("/sessions/{session_id}")
def patch_session(
    request: Request,
    session_id: str,
    body: dict,
    principal: dict = Depends(principal_dep),
) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        s = _session(request, uow, session_id)
        expected = body.get("expected_version")
        if expected is not None and expected != s.get("version"):
            raise ApiError(
                409,
                code="version_conflict",
                category="concurrency",
                message="session has changed since your read",
            )
        changes = {
            k: v
            for k, v in {
                "title": body.get("title"),
                "labels": body.get("labels"),
            }.items()
            if v is not None
        }
        if changes:
            uow.sessions.update(s["workspace_id"], session_id, changes, expected_version=expected)
        uow.commit()
        row = uow.sessions.get(s["workspace_id"], session_id)
        return {"session": views.session_view(row)}


@router.post("/sessions/{session_id}/closures")
def close_session(
    request: Request,
    session_id: str,
    body: dict | None = None,
    principal: dict = Depends(principal_dep),
) -> dict:
    svc = request.app.state.sessions
    with SqlUnitOfWork(request.app.state.db) as uow:
        s = _session(request, uow, session_id)
        out = svc.close_session(
            principal=principal, workspace_id=s["workspace_id"], session_id=session_id
        )
        uow.commit()
        return out


@router.post("/sessions/{session_id}/continuations", status_code=201)
def continue_session(
    request: Request,
    session_id: str,
    body: dict,
    principal: dict = Depends(principal_dep),
) -> dict:
    require_idem(request)
    svc = request.app.state.sessions
    with SqlUnitOfWork(request.app.state.db) as uow:
        s = _session(request, uow, session_id)
        ws = s["workspace_id"]
        replay = idem_replay(uow, request, workspace_id=ws, body=body)
        if replay is not None:
            return replay["response"]
        new_s = svc.create_session(
            principal=principal,
            workspace_id=ws,
            role=s.get("role") or "author",
            title=body.get("title") or s.get("title"),
            project_version_id=s.get("project_version_id"),
            projectless_spec=body.get("projectless_spec") or s.get("projectless_spec"),
            harness=body.get("harness") or s["harness"],
            effective_input=body.get("effective_input") or s.get("effective_input") or {},
            effective_input_digest=s.get("effective_input_digest") or "sha256:0",
            linked_from_session_id=session_id,
        )
        row = uow.sessions.get(ws, new_s["session_id"])
        resp = {"session": views.session_view(row), "continued_from": session_id}
        idem_record(
            uow,
            request,
            workspace_id=ws,
            body=body,
            status=201,
            response=resp,
            resource_ids={"session_id": new_s["session_id"]},
        )
        uow.commit()
        return resp


# ---- messages / turns / events --------------------------------------------


@router.get("/sessions/{session_id}/messages")
def list_messages(
    request: Request,
    session_id: str,
    principal: dict = Depends(principal_dep),
    limit: int = 100,
    cursor: str | None = None,
) -> dict:
    lim, off = pagination(limit=limit, cursor=cursor)
    with SqlUnitOfWork(request.app.state.db) as uow:
        s = _session(request, uow, session_id)
        rows = uow.messages.list_by_session(s["workspace_id"], session_id)
    items = rows[off : off + lim]
    out = {"items": [views.message_view(m) for m in items]}
    if off + lim < len(rows):
        out["next_cursor"] = str(off + lim)
    return out


@router.post("/sessions/{session_id}/messages", status_code=202)
def post_message(
    request: Request,
    session_id: str,
    body: dict,
    principal: dict = Depends(principal_dep),
) -> dict:
    """Accept input under the session lock — ids are returned before any
    provisioning (RFC 08)."""
    require_idem(request)
    svc = request.app.state.sessions
    with SqlUnitOfWork(request.app.state.db) as uow:
        s = _session(request, uow, session_id)
        ws = s["workspace_id"]
        replay = idem_replay(uow, request, workspace_id=ws, body=body)
        if replay is not None:
            return replay["response"]
        out = svc.post_message(
            principal=principal,
            workspace_id=ws,
            session_id=session_id,
            author=body.get("author") or {"kind": "user", "id": principal["id"]},
            routing=body.get("routing", "queue"),
            content=body.get("content") or {},
            role=body.get("role", "user"),
            attachment_refs=body.get("attachment_refs"),
            reply_to_message_id=body.get("reply_to_message_id"),
        )
        resp = {
            "accepted": True,
            "message_id": out["message_id"],
            "turn_id": out.get("turn_id"),
        }
        idem_record(
            uow,
            request,
            workspace_id=ws,
            body=body,
            status=202,
            response=resp,
            resource_ids=resp,
        )
        uow.commit()
        return resp


@router.get("/sessions/{session_id}/turns")
def list_turns(request: Request, session_id: str, principal: dict = Depends(principal_dep)) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        s = _session(request, uow, session_id)
        rows = uow.turns.list_by_session(s["workspace_id"], session_id)
    return {"items": [views.turn_view(t) for t in rows]}


@router.get("/turns/{turn_id}")
def get_turn(request: Request, turn_id: str, principal: dict = Depends(principal_dep)) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        for ws in request.state.principal_obj.workspace_ids:
            t = uow.turns.get(ws, turn_id)
            if t is not None:
                return views.turn_view(t)
    raise _nf("turn")


@router.post("/turns/{turn_id}/cancellations")
def cancel_turn(
    request: Request,
    turn_id: str,
    body: dict | None = None,
    principal: dict = Depends(principal_dep),
) -> dict:
    exec_svc = getattr(request.app.state, "execution", None)
    if exec_svc is None:
        raise unsupported("turn cancellation (runtime plane)")
    with SqlUnitOfWork(request.app.state.db) as uow:
        for ws in request.state.principal_obj.workspace_ids:
            t = uow.turns.get(ws, turn_id)
            if t is not None:
                out = exec_svc.cancel_turn(uow, workspace_id=ws, turn_id=turn_id)
                uow.commit()
                return out
    raise _nf("turn")


@router.get("/sessions/{session_id}/events")
def list_events(
    request: Request,
    session_id: str,
    principal: dict = Depends(principal_dep),
    after_seq: int = 0,
    types: str | None = None,
    turn_id: str | None = None,
    limit: int = 200,
) -> dict:
    """Committed replay — the event stream, not a buffered live feed."""
    if limit < 1 or limit > 1000:
        raise ApiError(
            422,
            code="validation_failed",
            category="request",
            message="limit must be 1..1000",
        )
    type_list = [t for t in (types or "").split(",") if t] or None
    with SqlUnitOfWork(request.app.state.db) as uow:
        s = _session(request, uow, session_id)
        rows, watermark = uow.events.list(
            s["workspace_id"],
            session_id,
            after_seq=after_seq,
            types=type_list,
            turn_id=turn_id,
            limit=limit,
        )
    return {
        "items": [views.event_view(e) for e in rows],
        "event_watermark": watermark,
    }


@router.get("/sessions/{session_id}/executor")
def session_executor(
    request: Request, session_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        s = _session(request, uow, session_id)
        ws = s["workspace_id"]
        lease = uow.leases.active_by_session(ws, session_id)
        leases = uow.rows.all(
            "SELECT * FROM executor_leases WHERE session_id=%s ORDER BY generation",
            (session_id,),
        )
        return {
            "active_lease": views.lease_view(lease) if lease else None,
            "leases": [views.lease_view(row) for row in leases],
            "worktree": _safe(
                uow.worktrees.get_by_session(ws, session_id),
                ("id", "state", "generation", "repository", "base_sha"),
            ),
        }


def _live_op(request: Request, session_id: str, kind: OperationKind, payload: dict) -> dict:
    """Submit a read-only operation to the session's live lease."""
    stack = getattr(request.app.state, "runtime_stack", None)
    if stack is None:
        raise unsupported("live worktree access (runtime plane)")
    with SqlUnitOfWork(request.app.state.db) as uow:
        s = _session(request, uow, session_id)
        ws = s["workspace_id"]
        lease = uow.leases.active_by_session(ws, session_id)
        uow.commit()
    if lease is None or not stack.pool.alive(lease["id"]):
        raise ApiError(
            503,
            code="executor_unavailable",
            category="executor",
            message="session has no live executor lease",
            retryable=True,
        )
    env = OperationEnvelope(
        operation_id=new_id("effect"),
        operation_kind=kind,
        session_id=session_id,
        lease_id=lease["id"],
        lease_generation=lease["generation"],
        payload=payload,
    )
    try:
        reply = stack.pool.submit_for_result(lease["id"], env.to_dict(), timeout=60.0)
    except Exception as exc:
        raise ApiError(
            503,
            code="executor_unavailable",
            category="executor",
            message=f"runtime operation failed: {type(exc).__name__}",
            retryable=True,
        ) from exc
    if reply.get("state") != "succeeded":
        result = reply.get("result") or {}
        raise ApiError(
            422,
            code=result.get("error_code") or "capture_failed",
            category="executor",
            message=result.get("message") or "operation failed on executor",
        )
    return reply.get("result") or {}


@router.get("/sessions/{session_id}/files")
def list_files(
    request: Request,
    session_id: str,
    path: str = ".",
    principal: dict = Depends(principal_dep),
) -> dict:
    result = _live_op(
        request,
        session_id,
        OperationKind.FILES_LIST,
        {"path": path, "root": "worktree"},
    )
    return {"entries": result.get("entries") or [], "path": path}


@router.get("/sessions/{session_id}/files/content")
def read_file(
    request: Request,
    session_id: str,
    path: str,
    principal: dict = Depends(principal_dep),
) -> dict:
    result = _live_op(
        request,
        session_id,
        OperationKind.FILES_READ,
        {"path": path, "root": "worktree"},
    )
    return {"path": path, "content_b64": result.get("content_b64")}


@router.get("/sessions/{session_id}/changes")
def observe_changes(
    request: Request, session_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    result = _live_op(request, session_id, OperationKind.CHANGES_OBSERVE, {})
    return {"entries": result.get("entries") or []}


@router.post("/sessions/{session_id}/changes", status_code=202)
def capture_changes(
    request: Request,
    session_id: str,
    body: dict | None = None,
    principal: dict = Depends(principal_dep),
) -> dict:
    require_idem(request)
    body = body or {}
    svc = request.app.state.changes
    with SqlUnitOfWork(request.app.state.db) as uow:
        s = _session(request, uow, session_id)
        ws = s["workspace_id"]
        replay = idem_replay(uow, request, workspace_id=ws, body=body)
        if replay is not None:
            return replay["response"]
        out = svc.request_capture(
            uow,
            workspace_id=ws,
            session_id=session_id,
            source_turn_id=body.get("source_turn_id"),
            origin=body.get("origin", "explicit"),
            baseline=body.get("baseline"),
        )
        resp = {"accepted": True, **out}
        idem_record(
            uow, request, workspace_id=ws, body=body, status=202, response=resp, resource_ids=out
        )
        uow.commit()
        return resp


@router.post("/sessions/{session_id}/terminals")
def create_terminal(
    request: Request, session_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    raise unsupported("interactive terminals")


@router.get("/sessions/{session_id}/services")
def list_services(
    request: Request, session_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        _session(request, uow, session_id)
        rows = uow.rows.all("SELECT * FROM service_instances WHERE session_id=%s", (session_id,))
    return {
        "items": [
            {
                "id": r["id"],
                "service_id": r.get("service_id"),
                "state": r.get("state"),
                "url": r.get("url"),
            }
            for r in rows
        ]
    }


# ---- changesets -----------------------------------------------------------


@router.get("/sessions/{session_id}/changesets")
def list_changesets(
    request: Request, session_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        s = _session(request, uow, session_id)
        rows = uow.changesets.list_by_session(s["workspace_id"], session_id)
    return {"items": [views.changeset_view(c) for c in rows]}


@router.get("/changesets/{changeset_id}")
def get_changeset(
    request: Request, changeset_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        for ws in request.state.principal_obj.workspace_ids:
            c = uow.changesets.get(ws, changeset_id)
            if c is not None:
                return views.changeset_view(c)
    raise _nf("changeset")


@router.get("/changesets/{changeset_id}/files")
def changeset_files(
    request: Request, changeset_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        for ws in request.state.principal_obj.workspace_ids:
            c = uow.changesets.get(ws, changeset_id)
            if c is not None:
                rows = uow.changeset_files.list_for(changeset_id)
                return {"items": [views.changeset_file_view(f) for f in rows]}
    raise _nf("changeset")


@router.post("/changesets/{changeset_id}/applications", status_code=202)
def apply_changeset(
    request: Request,
    changeset_id: str,
    body: dict,
    principal: dict = Depends(principal_dep),
) -> dict:
    require_idem(request)
    svc = request.app.state.changes
    with SqlUnitOfWork(request.app.state.db) as uow:
        found = None
        for ws in request.state.principal_obj.workspace_ids:
            c = uow.changesets.get(ws, changeset_id)
            if c is not None:
                found = (ws, c)
                break
        if found is None:
            raise _nf("changeset")
        ws, c = found
        replay = idem_replay(uow, request, workspace_id=ws, body=body)
        if replay is not None:
            return replay["response"]
        out = svc.apply(
            uow,
            workspace_id=ws,
            changeset_id=changeset_id,
            dest_session_id=body["session_id"],
        )
        resp = {"accepted": True, **out}
        idem_record(
            uow, request, workspace_id=ws, body=body, status=202, response=resp, resource_ids=out
        )
        uow.commit()
        return resp


@router.post("/changesets/{changeset_id}/deliveries", status_code=201)
def create_delivery(
    request: Request,
    changeset_id: str,
    body: dict,
    principal: dict = Depends(principal_dep),
) -> dict:
    require_idem(request)
    svc = request.app.state.delivery
    with SqlUnitOfWork(request.app.state.db) as uow:
        found = None
        for ws in request.state.principal_obj.workspace_ids:
            c = uow.changesets.get(ws, changeset_id)
            if c is not None:
                found = (ws, c)
                break
        if found is None:
            raise _nf("changeset")
        ws, c = found
        replay = idem_replay(uow, request, workspace_id=ws, body=body)
        if replay is not None:
            return replay["response"]
        d = svc.create(
            uow,
            workspace_id=ws,
            session_id=body.get("session_id") or c["session_id"],
            changeset_id=changeset_id,
            target=body.get("target") or {},
            transport=body.get("transport", "pull_request"),
            ship_policy=body.get("ship_policy"),
            authorizing_principal=principal["id"],
            connection_id=body.get("connection_id"),
        )
        steps = uow.delivery_steps.list_for(ws, d["id"])
        resp = {"delivery": views.delivery_view(d, steps=steps)}
        idem_record(
            uow,
            request,
            workspace_id=ws,
            body=body,
            status=201,
            response=resp,
            resource_ids={"delivery_id": d["id"]},
        )
        uow.commit()
        return resp


# ---- deliveries ------------------------------------------------------------


def _delivery(request: Request, uow: SqlUnitOfWork, delivery_id: str) -> tuple:
    for ws in request.state.principal_obj.workspace_ids:
        d = uow.deliveries.get(ws, delivery_id)
        if d is not None:
            return ws, d
    raise _nf("delivery")


@router.get("/deliveries/{delivery_id}")
def get_delivery(
    request: Request, delivery_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    svc = request.app.state.delivery
    with SqlUnitOfWork(request.app.state.db) as uow:
        ws, d = _delivery(request, uow, delivery_id)
        steps = uow.delivery_steps.list_for(ws, delivery_id)
        gate = svc.merge_gate(uow, workspace_id=ws, delivery_id=delivery_id)
        mrs = uow.rows.all(
            "SELECT * FROM merge_requests WHERE delivery_id=%s ORDER BY created_at",
            (delivery_id,),
        )
        out = views.delivery_view(d, steps=steps, gate=gate)
        out["merge_requests"] = [views.merge_request_view(m) for m in mrs]
        return out


@router.post("/deliveries/{delivery_id}/retries", status_code=202)
def retry_delivery(
    request: Request,
    delivery_id: str,
    body: dict | None = None,
    principal: dict = Depends(principal_dep),
) -> dict:
    require_idem(request)
    with SqlUnitOfWork(request.app.state.db) as uow:
        ws, d = _delivery(request, uow, delivery_id)
        if d["state"] == "succeeded":
            raise ApiError(
                409,
                code="invalid_state",
                category="state",
                message="delivery already succeeded",
            )
        job = enqueue_job(
            uow,
            workspace_id=ws,
            kind=JobKind.DELIVERY_PERFORM,
            target_family=TargetFamily.DELIVERY,
            target_id=delivery_id,
            dedupe_key=f"delivery.perform:{delivery_id}:retry:{d['version']}",
            payload={"delivery_id": delivery_id},
        )
        uow.commit()
        return {"accepted": True, "job": views.job_view(job)}


@router.post("/deliveries/{delivery_id}/merge-requests", status_code=201)
def request_merge(
    request: Request,
    delivery_id: str,
    body: dict,
    principal: dict = Depends(principal_dep),
) -> dict:
    require_idem(request)
    svc = request.app.state.delivery
    with SqlUnitOfWork(request.app.state.db) as uow:
        ws, _d = _delivery(request, uow, delivery_id)
        replay = idem_replay(uow, request, workspace_id=ws, body=body)
        if replay is not None:
            return replay["response"]
        out = svc.request_merge(
            uow,
            workspace_id=ws,
            delivery_id=delivery_id,
            expected_head_sha=body.get("expected_head_sha", ""),
            merge_method=body.get("merge_method", "squash"),
            authorizing_principal=principal["id"],
        )
        mr = uow.merge_requests.get(ws, out["merge_request_id"])
        resp = {"merge_request": views.merge_request_view(mr)}
        idem_record(
            uow, request, workspace_id=ws, body=body, status=201, response=resp, resource_ids=out
        )
        uow.commit()
        return resp


# ---- delegations -----------------------------------------------------------


@router.get("/sessions/{session_id}/delegations")
def list_delegations(
    request: Request, session_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        s = _session(request, uow, session_id)
        rows = uow.delegations.children_of(s["workspace_id"], session_id)
    return {"items": [views.delegation_view(d) for d in rows]}


@router.post("/sessions/{session_id}/delegations", status_code=201)
def spawn_delegation(
    request: Request,
    session_id: str,
    body: dict,
    principal: dict = Depends(principal_dep),
) -> dict:
    require_idem(request)
    svc = request.app.state.delegations
    harness = _harness_ok(body) if body.get("harness") else None
    with SqlUnitOfWork(request.app.state.db) as uow:
        s = _session(request, uow, session_id)
        ws = s["workspace_id"]
        replay = idem_replay(uow, request, workspace_id=ws, body=body)
        if replay is not None:
            return replay["response"]
        d = svc.spawn(
            uow,
            principal=principal,
            workspace_id=ws,
            parent_session_id=session_id,
            role=body.get("role", "reviewer"),
            result_contract=body.get("result_contract") or {"kind": "GenericResult"},
            inputs=body.get("inputs") or [],
            prompt=body.get("prompt", ""),
            harness=harness or s["harness"],
            budget=body.get("budget"),
            project_version_id=s.get("project_version_id"),
            projectless_spec=s.get("projectless_spec"),
        )
        row = uow.delegations.get(ws, d["delegation_id"])
        resp = {"delegation": views.delegation_view(row)}
        resp["delegation"]["message_id"] = d.get("message_id")
        resp["delegation"]["turn_id"] = d.get("turn_id")
        idem_record(
            uow,
            request,
            workspace_id=ws,
            body=body,
            status=201,
            response=resp,
            resource_ids={"delegation_id": d["delegation_id"]},
        )
        uow.commit()
        return resp


def _delegation(request: Request, uow: SqlUnitOfWork, delegation_id: str) -> tuple:
    for ws in request.state.principal_obj.workspace_ids:
        d = uow.delegations.get(ws, delegation_id)
        if d is not None:
            return ws, d
    raise _nf("delegation")


@router.get("/delegations/{delegation_id}")
def get_delegation(
    request: Request, delegation_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        ws, d = _delegation(request, uow, delegation_id)
        inputs = uow.rows.all(
            "SELECT * FROM delegation_inputs WHERE delegation_id=%s",
            (delegation_id,),
        )
        out = views.delegation_view(d, inputs=inputs)
        out["result"] = views.delegation_result_view(
            uow.delegation_results.get_for(ws, delegation_id)
        )
        out["waits"] = [
            views.wait_view(w)
            for w in uow.rows.all(
                "SELECT * FROM wait_subscriptions WHERE delegation_id=%s",
                (delegation_id,),
            )
        ]
        return out


@router.get("/delegations/{delegation_id}/result")
def delegation_result(
    request: Request, delegation_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        ws, _d = _delegation(request, uow, delegation_id)
        r = uow.delegation_results.get_for(ws, delegation_id)
        if r is None:
            raise ApiError(
                409,
                code="invalid_state",
                category="state",
                message="delegation has no published result",
            )
        return views.delegation_result_view(r)


@router.post("/delegations/{delegation_id}/waits", status_code=201)
def wait_delegation(
    request: Request,
    delegation_id: str,
    body: dict,
    principal: dict = Depends(principal_dep),
) -> dict:
    svc = request.app.state.delegations
    with SqlUnitOfWork(request.app.state.db) as uow:
        ws, d = _delegation(request, uow, delegation_id)
        out = svc.wait(
            uow,
            workspace_id=ws,
            subscriber_session_id=body.get("subscriber_session_id") or d["parent_session_id"],
            delegation_id=delegation_id,
            predicate=body.get("predicate"),
            deadline_seconds=body.get("deadline_seconds", 1800),
        )
        uow.commit()
        w = uow.wait_subscriptions.get(ws, out["wait_id"]) if "wait_id" in out else None
        return {"wait": views.wait_view(w) if w else out}


@router.post("/delegations/{delegation_id}/cancellations")
def cancel_delegation(
    request: Request,
    delegation_id: str,
    body: dict | None = None,
    principal: dict = Depends(principal_dep),
) -> dict:
    svc = request.app.state.delegations
    with SqlUnitOfWork(request.app.state.db) as uow:
        ws, _d = _delegation(request, uow, delegation_id)
        exec_svc = getattr(request.app.state, "execution", None)
        out = svc.cancel(
            uow,
            workspace_id=ws,
            delegation_id=delegation_id,
            execution_service=exec_svc,
        )
        uow.commit()
        return out


# ---- jobs / operations -----------------------------------------------------


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str, principal: dict = Depends(principal_dep)) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        for ws in request.state.principal_obj.workspace_ids:
            j = uow.jobs.get(ws, job_id)
            if j is not None:
                return views.job_view(j)
    raise _nf("job")


@router.get("/operations/{operation_id}")
def get_operation(
    request: Request, operation_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    """A Job's evidence view — not a second authority."""
    with SqlUnitOfWork(request.app.state.db) as uow:
        for ws in request.state.principal_obj.workspace_ids:
            j = uow.jobs.find_by_effect(operation_id)
            if j is not None and j["workspace_id"] == ws:
                return views.operation_view(j)
            j = uow.jobs.get(ws, operation_id)
            if j is not None:
                return views.operation_view(j)
    raise _nf("operation")
