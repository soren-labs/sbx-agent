"""Unified /api FastAPI application (RFC 167 §08).

One business surface over the unified services — no /v1, /api/v2 or hosted
routers. Auth is cookie or Bearer (login token or ``sbx_k_`` API key).
Every handler scopes by owner workspace; secrets never leave responses.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from control.api import routes_identity, routes_resources, routes_sessions
from control.api.errors import ApiError, error_payload, from_domain
from control.domain.errors import DomainError


def create_app(
    db,
    *,
    auth=None,
    projects=None,
    connections=None,
    sessions=None,
    changes=None,
    delivery=None,
    delegations=None,
    models=None,
    runtime_stack=None,
    execution=None,
) -> FastAPI:
    """Build the unified app. Services are injected; a missing service
    yields honest 503/unsupported_capability rather than a stub."""
    app = FastAPI(title="sbx unified api", openapi_url=None)
    app.state.db = db
    app.state.auth = auth
    app.state.projects = projects
    app.state.connections = connections
    app.state.sessions = sessions
    app.state.changes = changes
    app.state.delivery = delivery
    app.state.delegations = delegations
    app.state.models = models
    app.state.runtime_stack = runtime_stack
    app.state.execution = execution

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request.state.request_id = (
            request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:16]}"
        )
        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status,
            content=error_payload(
                code=exc.code,
                category=exc.category,
                message=exc.message,
                retryable=exc.retryable,
                details=exc.details,
                request_id=getattr(request.state, "request_id", "req_unknown"),
            ),
        )

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        mapped = from_domain(exc)
        return JSONResponse(
            status_code=mapped.status,
            content=error_payload(
                code=mapped.code,
                category=mapped.category,
                message=mapped.message,
                retryable=mapped.retryable,
                details=mapped.details,
                request_id=getattr(request.state, "request_id", "req_unknown"),
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=error_payload(
                code="validation_failed",
                category="request",
                message="request validation failed",
                details={"errors": exc.errors()[:10]},
                request_id=getattr(request.state, "request_id", "req_unknown"),
            ),
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/readyz")
    def readyz() -> dict:
        try:
            conn = db.acquire(timeout=5.0)
            try:
                conn.execute("SELECT 1")
            finally:
                db.release(conn)
        except Exception as exc:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "reason": type(exc).__name__},
            )
        return {"ok": True}

    app.include_router(routes_identity.router)
    app.include_router(routes_resources.router)
    app.include_router(routes_sessions.router)
    return app
