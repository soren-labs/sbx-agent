"""ChangeSets, Deliveries, Delegations and the private tool gateway routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

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


@router.get("/api/sessions/{session_id}/changes")
def live_changes(request: Request, session_id: str) -> dict[str, Any]:
    return services(request).changes.live(principal(request), session_id)


@router.get("/api/sessions/{session_id}/changesets")
def list_changesets(request: Request, session_id: str) -> dict[str, Any]:
    return services(request).changes.list(principal(request), session_id)


@router.post("/api/sessions/{session_id}/changesets")
async def capture(request: Request, session_id: str) -> JSONResponse:
    who = mutating_principal(request)
    return accepted(
        services(request).changes.request_capture(
            who, session_id, await json_body(request), idempotency_key=idempotency_key(request)
        )
    )


@router.get("/api/changesets/{changeset_id}")
def get_changeset(request: Request, changeset_id: str) -> dict[str, Any]:
    return services(request).changes.get(principal(request), changeset_id)


@router.get("/api/changesets/{changeset_id}/files")
def changeset_file(request: Request, changeset_id: str, path: str | None = None) -> dict[str, Any]:
    svc = services(request).changes
    who = principal(request)
    if path:
        return svc.file(who, changeset_id, path)
    return {"items": svc.get(who, changeset_id)["files"]}


@router.get("/api/changesets/{changeset_id}/diff")
def changeset_diff(request: Request, changeset_id: str) -> dict[str, Any]:
    return services(request).changes.diff(principal(request), changeset_id)


@router.post("/api/changesets/{changeset_id}/applications")
async def apply_changeset(request: Request, changeset_id: str) -> JSONResponse:
    who = mutating_principal(request)
    return accepted(
        services(request).changes.request_apply(
            who, changeset_id, await json_body(request), idempotency_key=idempotency_key(request)
        )
    )


@router.get("/api/changesets/{changeset_id}/deliveries")
def list_deliveries(request: Request, changeset_id: str) -> dict[str, Any]:
    return services(request).deliveries.list(principal(request), changeset_id)


@router.post("/api/changesets/{changeset_id}/deliveries")
async def request_delivery(request: Request, changeset_id: str) -> JSONResponse:
    who = mutating_principal(request)
    return accepted(
        services(request).deliveries.request(
            who, changeset_id, await json_body(request), idempotency_key=idempotency_key(request)
        )
    )


@router.get("/api/deliveries/{delivery_id}")
def get_delivery(request: Request, delivery_id: str) -> dict[str, Any]:
    return services(request).deliveries.get(principal(request), delivery_id)


@router.post("/api/deliveries/{delivery_id}/retries")
def retry_delivery(request: Request, delivery_id: str) -> JSONResponse:
    return accepted(services(request).deliveries.retry(mutating_principal(request), delivery_id))


@router.post("/api/deliveries/{delivery_id}/refreshes")
def refresh_delivery(request: Request, delivery_id: str) -> JSONResponse:
    return accepted(services(request).deliveries.refresh(mutating_principal(request), delivery_id))


@router.post("/api/deliveries/{delivery_id}/merge-requests")
async def merge_request(request: Request, delivery_id: str) -> JSONResponse:
    who = mutating_principal(request)
    return accepted(
        services(request).deliveries.request_merge(
            who, delivery_id, await json_body(request), idempotency_key=idempotency_key(request)
        )
    )


@router.get("/api/sessions/{session_id}/delegations")
def list_delegations(request: Request, session_id: str) -> dict[str, Any]:
    return services(request).delegations.list(principal(request), session_id)


@router.post("/api/sessions/{session_id}/delegations")
async def spawn(request: Request, session_id: str) -> JSONResponse:
    who = mutating_principal(request)
    return accepted(
        services(request).delegations.spawn(
            who, session_id, await json_body(request), idempotency_key=idempotency_key(request)
        )
    )


@router.get("/api/delegations/{delegation_id}")
def get_delegation(request: Request, delegation_id: str) -> dict[str, Any]:
    return services(request).delegations.get(principal(request), delegation_id)


@router.get("/api/delegations/{delegation_id}/result")
def delegation_result(request: Request, delegation_id: str) -> dict[str, Any]:
    return services(request).delegations.result(principal(request), delegation_id)


@router.post("/api/delegations/{delegation_id}/result")
def submit_result(request: Request, delegation_id: str) -> dict[str, Any]:
    mutating_principal(request)
    raise DomainError(
        "forbidden",
        "results are published by the platform from the child's validated completing Turn",
    )


@router.post("/api/delegations/{delegation_id}/messages")
async def delegation_message(request: Request, delegation_id: str) -> JSONResponse:
    who = mutating_principal(request)
    return accepted(
        services(request).delegations.send(
            who, delegation_id, await json_body(request), idempotency_key=idempotency_key(request)
        )
    )


@router.post("/api/delegations/{delegation_id}/waits")
async def wait(request: Request, delegation_id: str) -> dict[str, Any]:
    who = mutating_principal(request)
    return services(request).delegations.wait(
        who, delegation_id, await json_body(request), idempotency_key=idempotency_key(request)
    )


@router.post("/api/delegations/{delegation_id}/cancellations")
def cancel_delegation(request: Request, delegation_id: str) -> dict[str, Any]:
    return services(request).delegations.cancel(mutating_principal(request), delegation_id)


@router.post("/internal/tools/{tool}")
async def tool_call(request: Request, tool: str) -> dict[str, Any]:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("SBX-Tool "):
        raise DomainError("unauthenticated", "tool grant required")
    body = await json_body(request)
    return services(request).tools.call(
        auth.split(" ", 1)[1], tool, str(body.get("operation_id") or ""), body.get("args") or {}
    )
