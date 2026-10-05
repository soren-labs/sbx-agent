"""Files, terminal, services/preview and operation status routes."""

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
from control.application import access
from control.domain.errors import DomainError

router = APIRouter()


@router.get("/api/sessions/{session_id}/files")
def list_files(request: Request, session_id: str, path: str = "") -> dict[str, Any]:
    return services(request).live.list_files(principal(request), session_id, path)


@router.get("/api/sessions/{session_id}/files/content")
def read_file(request: Request, session_id: str, path: str) -> dict[str, Any]:
    return services(request).live.read_file(principal(request), session_id, path)


@router.put("/api/sessions/{session_id}/files")
async def write_file(request: Request, session_id: str) -> dict[str, Any]:
    who = mutating_principal(request)
    return services(request).live.write_file(
        who, session_id, await json_body(request), idempotency_key=idempotency_key(request) or ""
    )


@router.post("/api/sessions/{session_id}/file-uploads")
def file_upload(request: Request, session_id: str) -> dict[str, Any]:
    mutating_principal(request)
    raise DomainError(
        "unsupported_capability", "upload intents are not enabled; use PUT /files for text content"
    )


@router.post("/api/sessions/{session_id}/terminals", status_code=201)
def create_terminal(request: Request, session_id: str) -> dict[str, Any]:
    return services(request).live.create_terminal(mutating_principal(request), session_id)


@router.post("/api/sessions/{session_id}/terminals/{terminal_id}/input")
async def terminal_input(request: Request, session_id: str, terminal_id: str) -> dict[str, Any]:
    who = mutating_principal(request)
    body = await json_body(request)
    return services(request).live.terminal_input(
        who, session_id, terminal_id, str(body.get("data") or "")
    )


@router.get("/api/sessions/{session_id}/terminals/{terminal_id}/output")
def terminal_output(
    request: Request, session_id: str, terminal_id: str, after: int = 0
) -> dict[str, Any]:
    return services(request).live.terminal_output(
        principal(request), session_id, terminal_id, after
    )


@router.get("/api/sessions/{session_id}/services")
def list_services(request: Request, session_id: str) -> dict[str, Any]:
    return services(request).services.list(principal(request), session_id)


@router.post("/api/sessions/{session_id}/services/{name}/activations")
def activate_service(request: Request, session_id: str, name: str) -> JSONResponse:
    return JSONResponse(
        services(request).services.set_desired(
            mutating_principal(request), session_id, name, "running"
        ),
        status_code=202,
    )


@router.post("/api/sessions/{session_id}/services/{name}/stops")
def stop_service(request: Request, session_id: str, name: str) -> JSONResponse:
    return JSONResponse(
        services(request).services.set_desired(
            mutating_principal(request), session_id, name, "stopped"
        ),
        status_code=202,
    )


@router.get("/api/sessions/{session_id}/services/{name}/logs")
def service_logs(request: Request, session_id: str, name: str) -> dict[str, Any]:
    return services(request).services.logs(principal(request), session_id, name)


@router.post("/api/sessions/{session_id}/services/{name}/preview-grants")
def preview_grant(request: Request, session_id: str, name: str) -> dict[str, Any]:
    mutating_principal(request)
    raise DomainError(
        "unsupported_capability",
        "preview requires a dedicated preview origin, which this deployment does not configure",
        details={"capability": "preview"},
    )


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "kind": job["kind"],
        "state": job["state"],
        "attempts": job["attempts"],
        "last_error_code": job["last_error_code"],
        "due_at": job["due_at"].isoformat(),
        "created_at": job["created_at"].isoformat(),
        "finished_at": job["finished_at"].isoformat() if job["finished_at"] else None,
    }


@router.get("/api/jobs/{job_id}")
def get_job(request: Request, job_id: str) -> dict[str, Any]:
    who = principal(request)
    return services(request).tx.read(
        lambda uow: _job_view(access.owned(uow, who, "jobs", job_id, what="job"))
    )


@router.get("/api/operations/{operation_id}")
def get_operation(request: Request, operation_id: str) -> dict[str, Any]:
    """Projects Job/attempt evidence for an operation; not another authority."""
    who = principal(request)

    def fn(uow: Any) -> dict[str, Any]:
        job = access.owned(uow, who, "jobs", operation_id, what="operation")
        attempts = uow.find("job_attempts", {"job_id": job["id"]}, order="generation")
        return {
            **_job_view(job),
            "attempts": [
                {
                    "generation": a["generation"],
                    "outcome": a["outcome"],
                    "error_code": a["error_code"],
                    "reclaimed": a["reclaimed"],
                }
                for a in attempts
            ],
        }

    return services(request).tx.read(fn)
