"""Canonical error responses. Request bodies are never echoed (secrets are write-only)."""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from control.domain.errors import DomainError

log = logging.getLogger("sbx.api")


def request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if rid is None:
        rid = "req_" + uuid.uuid4().hex[:16]
        request.state.request_id = rid
    return rid


def install(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        headers = {"Retry-After": str(int(exc.retry_after))} if exc.retry_after else None
        return JSONResponse(
            exc.to_dict(request_id(request)), status_code=exc.status, headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()]
        err = DomainError(
            "validation_failed", "request validation failed", details={"fields": fields}
        )
        return JSONResponse(err.to_dict(request_id(request)), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "not_found", 401: "unauthenticated", 403: "forbidden"}.get(
            exc.status_code, "validation_failed"
        )
        err = DomainError(
            code, str(exc.detail) if exc.status_code != 404 else "not found", status=exc.status_code
        )
        return JSONResponse(err.to_dict(request_id(request)), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error %s", request_id(request))
        err = DomainError("internal_error", "internal error")
        return JSONResponse(err.to_dict(request_id(request)), status_code=500)
