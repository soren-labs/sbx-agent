"""Projects, Connections, Sessions, Messages, Turns, Events, Executor and catalog routes."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from control.api.dependencies import (
    idempotency_key,
    json_body,
    mutating_principal,
    principal,
    services,
)
from control.domain.errors import DomainError

router = APIRouter()


def accepted(body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(body, status_code=202)


# -- Projects ---------------------------------------------------------------------------
@router.get("/api/workspaces/{workspace_id}/projects")
def list_projects(request: Request, workspace_id: str) -> dict[str, Any]:
    return services(request).projects.list(principal(request), workspace_id)


@router.post("/api/workspaces/{workspace_id}/projects", status_code=201)
async def create_project(request: Request, workspace_id: str) -> dict[str, Any]:
    who = mutating_principal(request)
    return services(request).projects.create(
        who, workspace_id, await json_body(request), idempotency_key=idempotency_key(request)
    )


@router.get("/api/projects/{project_id}")
def get_project(request: Request, project_id: str) -> dict[str, Any]:
    return services(request).projects.get(principal(request), project_id)


@router.patch("/api/projects/{project_id}")
async def patch_project(request: Request, project_id: str) -> dict[str, Any]:
    who = mutating_principal(request)
    return services(request).projects.update(who, project_id, await json_body(request))


@router.get("/api/projects/{project_id}/versions")
def project_versions(request: Request, project_id: str) -> dict[str, Any]:
    return services(request).projects.versions(principal(request), project_id)


@router.post("/api/projects/{project_id}/versions", status_code=201)
async def publish_version(request: Request, project_id: str) -> dict[str, Any]:
    who = mutating_principal(request)
    return services(request).projects.publish_version(
        who, project_id, await json_body(request), idempotency_key=idempotency_key(request)
    )


# -- Connections ------------------------------------------------------------------------
@router.get("/api/workspaces/{workspace_id}/connections")
def list_connections(
    request: Request, workspace_id: str, include_revoked: bool = False
) -> dict[str, Any]:
    return services(request).connections.list(
        principal(request), workspace_id, include_revoked=include_revoked
    )


@router.post("/api/workspaces/{workspace_id}/connections", status_code=201)
async def create_connection(request: Request, workspace_id: str) -> dict[str, Any]:
    who = mutating_principal(request)
    return services(request).connections.create(
        who, workspace_id, await json_body(request), idempotency_key=idempotency_key(request)
    )


@router.get("/api/connections/{connection_id}")
def get_connection(request: Request, connection_id: str) -> dict[str, Any]:
    return services(request).connections.get(principal(request), connection_id)


@router.patch("/api/connections/{connection_id}")
async def patch_connection(request: Request, connection_id: str) -> dict[str, Any]:
    who = mutating_principal(request)
    return services(request).connections.update(who, connection_id, await json_body(request))


@router.delete("/api/connections/{connection_id}")
def disconnect(request: Request, connection_id: str) -> dict[str, Any]:
    return services(request).connections.disconnect(mutating_principal(request), connection_id)


@router.post("/api/connections/{connection_id}/credential-versions", status_code=201)
async def replace_credential(request: Request, connection_id: str) -> dict[str, Any]:
    who = mutating_principal(request)
    return services(request).connections.replace(
        who, connection_id, await json_body(request), idempotency_key=idempotency_key(request)
    )


@router.get("/api/connections/{connection_id}/validations")
def validations(request: Request, connection_id: str) -> dict[str, Any]:
    view = services(request).connections.get(principal(request), connection_id)
    return {"connection_id": connection_id, "health": view["health"], "latest": view["validation"]}


@router.post("/api/connections/{connection_id}/validations")
def validate(request: Request, connection_id: str) -> JSONResponse:
    return accepted(
        services(request).connections.request_validation(mutating_principal(request), connection_id)
    )


@router.get("/api/connections/{connection_id}/capabilities")
def capabilities(request: Request, connection_id: str) -> dict[str, Any]:
    return services(request).connections.capabilities(principal(request), connection_id)


# -- Catalog ------------------------------------------------------------------------------
@router.get("/api/harnesses")
def harnesses(request: Request) -> dict[str, Any]:
    principal(request)
    return {"items": services(request).catalog.providers()}


@router.get("/api/executor-backends")
def executor_backends(request: Request) -> dict[str, Any]:
    principal(request)
    svc = services(request)
    return {"items": [{"kind": k, **b.capabilities()} for k, b in svc.execution.executors.items()]}


@router.get("/api/models")
def models(
    request: Request, workspace_id: str | None = None, provider_id: str = "opencode"
) -> dict[str, Any]:
    who = principal(request)
    svc = services(request)
    manifest = svc.catalog.manifest(provider_id) or {}
    return svc.connections.models(
        who,
        workspace_id or who.default_workspace_id,
        provider_id=provider_id,
        accepted=manifest.get("inference_protocols") or [],
    )


# -- Sessions -------------------------------------------------------------------------------
@router.get("/api/workspaces/{workspace_id}/sessions")
def list_sessions(
    request: Request,
    workspace_id: str,
    lifecycle: str | None = None,
    role: str | None = None,
    project_id: str | None = None,
    parent_session_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    return services(request).queries.sessions(
        principal(request),
        workspace_id,
        lifecycle=lifecycle,
        role=role,
        project_id=project_id,
        parent_session_id=parent_session_id,
        limit=limit,
        cursor=cursor,
    )


@router.post("/api/workspaces/{workspace_id}/sessions")
async def create_session(request: Request, workspace_id: str) -> JSONResponse:
    who = mutating_principal(request)
    body = await json_body(request)
    result = services(request).sessions.create(
        who, workspace_id, body, idempotency_key=idempotency_key(request)
    )
    return JSONResponse(result, status_code=202 if result.get("turn_id") else 201)


@router.get("/api/sessions/{session_id}")
def get_session(request: Request, session_id: str) -> dict[str, Any]:
    return services(request).queries.session(principal(request), session_id)


@router.patch("/api/sessions/{session_id}")
async def patch_session(request: Request, session_id: str) -> dict[str, Any]:
    who = mutating_principal(request)
    return services(request).sessions.update(who, session_id, await json_body(request))


@router.get("/api/sessions/{session_id}/messages")
def list_messages(request: Request, session_id: str) -> dict[str, Any]:
    return services(request).queries.messages(principal(request), session_id)


@router.post("/api/sessions/{session_id}/messages")
async def send_message(request: Request, session_id: str) -> JSONResponse:
    who = mutating_principal(request)
    body = await json_body(request)
    return accepted(
        services(request).sessions.send(
            who, session_id, body, idempotency_key=idempotency_key(request)
        )
    )


@router.get("/api/sessions/{session_id}/turns")
def list_turns(request: Request, session_id: str) -> dict[str, Any]:
    return services(request).queries.turns(principal(request), session_id)


@router.get("/api/turns/{turn_id}")
def get_turn(request: Request, turn_id: str) -> dict[str, Any]:
    return services(request).queries.turn(principal(request), turn_id)


@router.post("/api/turns/{turn_id}/cancellations")
def cancel_turn(request: Request, turn_id: str) -> JSONResponse:
    who = mutating_principal(request)
    return accepted(
        services(request).sessions.cancel_turn(
            who, turn_id, idempotency_key=idempotency_key(request)
        )
    )


@router.post("/api/turns/{turn_id}/retries")
def retry_turn(request: Request, turn_id: str) -> JSONResponse:
    who = mutating_principal(request)
    return accepted(
        services(request).sessions.retry_turn(
            who, turn_id, idempotency_key=idempotency_key(request)
        )
    )


@router.post("/api/turns/{turn_id}/acknowledgements")
def acknowledge(request: Request, turn_id: str) -> dict[str, Any]:
    return services(request).sessions.acknowledge_unknown(mutating_principal(request), turn_id)


@router.post("/api/turns/{turn_id}/steering")
def steering(request: Request, turn_id: str) -> dict[str, Any]:
    mutating_principal(request)
    raise DomainError(
        "unsupported_capability",
        "no enabled Harness verifies steer injection; send a queued Message",
        details={"capability": "steer"},
    )


def _lifecycle(request: Request, session_id: str, target: str) -> dict[str, Any]:
    who = mutating_principal(request)
    return services(request).sessions.set_lifecycle(
        who, session_id, target, idempotency_key=idempotency_key(request)
    )


@router.post("/api/sessions/{session_id}/archives")
def archive(request: Request, session_id: str) -> dict[str, Any]:
    return _lifecycle(request, session_id, "archived")


@router.post("/api/sessions/{session_id}/unarchives")
def unarchive(request: Request, session_id: str) -> dict[str, Any]:
    return _lifecycle(request, session_id, "open")


@router.post("/api/sessions/{session_id}/closures")
def close(request: Request, session_id: str) -> dict[str, Any]:
    return _lifecycle(request, session_id, "closed")


@router.get("/api/sessions/{session_id}/executor")
def executor(request: Request, session_id: str) -> dict[str, Any]:
    return services(request).execution.executor_view(principal(request), session_id)


@router.post("/api/sessions/{session_id}/executor/activations")
def activate(request: Request, session_id: str) -> JSONResponse:
    return accepted(services(request).execution.activate(mutating_principal(request), session_id))


@router.post("/api/sessions/{session_id}/executor/releases")
def release(request: Request, session_id: str) -> JSONResponse:
    return accepted(services(request).execution.release(mutating_principal(request), session_id))


@router.get("/api/sessions/{session_id}/events", response_model=None)
def events(
    request: Request,
    session_id: str,
    after: int | None = None,
    limit: int = 500,
    types: str | None = None,
    turn_id: str | None = None,
    max_seconds: float = 600,
) -> Any:
    who = principal(request)
    queries = services(request).queries
    last_event_id = request.headers.get("last-event-id")
    if after is not None and last_event_id is not None and str(after) != last_event_id.strip():
        raise DomainError("invalid_cursor", "after and Last-Event-ID disagree")
    cursor = (
        after if after is not None else int(last_event_id) if (last_event_id or "").isdigit() else 0
    )
    type_list = [t for t in (types or "").split(",") if t] or None
    if "text/event-stream" not in request.headers.get("accept", ""):
        return queries.events(
            who, session_id, after=cursor, limit=limit, types=type_list, turn_id=turn_id
        )
    queries.events(
        who, session_id, after=cursor, limit=1
    )  # authorize + validate cursor before streaming

    def stream() -> Iterator[str]:
        position, last_beat, started = cursor, time.monotonic(), time.monotonic()
        while time.monotonic() - started < min(max(max_seconds, 0.5), 600):
            page = queries.events(
                who, session_id, after=position, limit=limit, types=type_list, turn_id=turn_id
            )
            for event in page["items"]:
                yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
            if page["next_after"] > position:
                position = page["next_after"]
                yield f": watermark {page['event_watermark']}\n\n"
                continue
            if time.monotonic() - last_beat > 15:
                last_beat = time.monotonic()
                yield ": heartbeat\n\n"
            time.sleep(0.4)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )
