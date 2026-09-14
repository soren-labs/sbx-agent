"""Canonical ``/v1`` error body and per-router exception mapping.

The app-level ``HTTPException`` handler in ``control.app`` emits the internal
``/api/*`` shape ``{error: <subcode>, code: <http>}``. The public v1 contract
(``docs/contracts/api-v1.yaml``) uses ``{error: {code, message, retry_after?}}``
instead, so the v1 router wraps every route in :class:`V1Route` which converts
:class:`V1ApiError` and request-validation failures before they reach the app
handler.
"""

from __future__ import annotations

from typing import Any

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException

# HTTP status -> canonical error_subcodes fallback for stray HTTPExceptions.
_STATUS_CODES = {
    400: "invalid_provider",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "turn_in_progress",
    429: "concurrency_limit",
}


def error_body(code: str, message: str, retry_after: float | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if retry_after is not None:
        error["retry_after"] = retry_after
    return {"error": error}


class V1ApiError(Exception):
    """An error that must be returned with the canonical v1 error body."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content=error_body(self.code, self.message, self.retry_after),
        )


def not_found(message: str = "resource not found") -> V1ApiError:
    return V1ApiError(404, "not_found", message)


def invalid_provider(message: str) -> V1ApiError:
    return V1ApiError(400, "invalid_provider", message)


class V1Route(APIRoute):
    """Route class translating v1-layer failures into the canonical error body."""

    def get_route_handler(self) -> Any:
        original = super().get_route_handler()

        async def handler(request: Any) -> Any:
            try:
                return await original(request)
            except V1ApiError as exc:
                return exc.response()
            except RequestValidationError:
                return JSONResponse(
                    status_code=400,
                    content=error_body("invalid_provider", "malformed request"),
                )
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                error = detail.get("error")
                if isinstance(error, dict) and "code" in error:
                    return JSONResponse(status_code=exc.status_code, content=detail)
                code = _STATUS_CODES.get(exc.status_code, "invalid_provider")
                message = str(exc.detail) if exc.detail else code
                return JSONResponse(
                    status_code=exc.status_code,
                    content=error_body(code, message),
                    headers=exc.headers,
                )

        return handler
