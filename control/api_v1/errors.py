"""Canonical ``/v1`` error body and per-router exception mapping.

The app-level ``HTTPException`` handler in ``control.app`` emits the internal
``/api/*`` shape ``{error: <subcode>, code: <http>}``. The public v1 contract
(``docs/contracts/api-v1.yaml``) uses the richer SOR-226 shape
``{error: {code, message, retryable, action, retry_after?, details?}}``
instead, so the v1 router wraps every route in :class:`V1Route` which converts
:class:`V1ApiError` and request-validation failures before they reach the app
handler.

``code`` values come from the canonical catalog in
:mod:`control.api_v1.error_catalog`; ``retryable``/``action`` are filled from
the catalog when a raise site does not override them. ``details`` carries
optional structured context (e.g. which request fields failed validation).
"""

from __future__ import annotations

from typing import Any

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException

from control.api_v1.error_catalog import spec_for

# HTTP status -> canonical error_subcodes fallback for stray HTTPExceptions.
_STATUS_CODES = {
    400: "invalid_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "turn_in_progress",
    429: "concurrency_limit",
    500: "internal",
    502: "repo_unavailable",
    503: "unavailable",
}


def error_body(
    code: str,
    message: str,
    retry_after: float | None = None,
    *,
    retryable: bool | None = None,
    action: str | None = None,
    details: dict[str, Any] | None = None,
    status: int | None = None,
) -> dict[str, Any]:
    """Canonical ``{error: {...}}`` body — the SOR-226 public shape.

    ``retryable``/``action`` default from the error catalog so every error
    body is complete; ``retry_after`` and ``details`` stay optional.
    """
    spec = spec_for(code, status)
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": spec.retryable if retryable is None else retryable,
        "action": spec.action if action is None else action,
    }
    if retry_after is not None:
        error["retry_after"] = retry_after
    if details:
        error["details"] = dict(details)
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
        retryable: bool | None = None,
        action: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.retryable = retryable
        self.action = action
        self.details = details

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content=error_body(
                self.code,
                self.message,
                self.retry_after,
                retryable=self.retryable,
                action=self.action,
                details=self.details,
                status=self.status_code,
            ),
        )


def not_found(message: str = "resource not found") -> V1ApiError:
    return V1ApiError(404, "not_found", message)


def invalid_request(message: str, *, details: dict[str, Any] | None = None) -> V1ApiError:
    """Malformed request that is not about the provider field (SOR-226)."""
    return V1ApiError(400, "invalid_request", message, details=details)


def invalid_provider(message: str) -> V1ApiError:
    """The ``provider`` value itself failed validation."""
    return V1ApiError(400, "invalid_provider", message)


def _validation_error(exc: RequestValidationError) -> JSONResponse:
    """Map a Pydantic validation failure onto the canonical code.

    Loc-aware: a body that only fails inside ``output_contract`` reports the
    dedicated code; failures confined to a ``provider`` field keep
    ``invalid_provider`` (the provider literal rejected the value); every
    other malformed request reports ``invalid_request``.
    """
    locs = [tuple(err.get("loc") or ()) for err in exc.errors()]
    details = {"loc": [[str(part) for part in loc] for loc in locs]}
    if locs and all("output_contract" in loc for loc in locs):
        return JSONResponse(
            status_code=400,
            content=error_body(
                "invalid_output_contract", "invalid output contract", details=details
            ),
        )
    if locs and all(loc[-1] == "provider" for loc in locs):
        return JSONResponse(
            status_code=400,
            content=error_body("invalid_provider", "invalid provider", details=details),
        )
    return JSONResponse(
        status_code=400,
        content=error_body("invalid_request", "malformed request", details=details),
    )


class V1Route(APIRoute):
    """Route class translating v1-layer failures into the canonical error body."""

    def get_route_handler(self) -> Any:
        original = super().get_route_handler()

        async def handler(request: Any) -> Any:
            try:
                return await original(request)
            except V1ApiError as exc:
                return exc.response()
            except RequestValidationError as exc:
                return _validation_error(exc)
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                error = detail.get("error")
                if isinstance(error, dict) and "code" in error:
                    # Canonical-shaped detail passes through; missing catalog
                    # fields are filled so the body is always complete.
                    code = str(error["code"])
                    content = dict(detail)
                    content["error"] = error_body(
                        code,
                        str(error.get("message") or code),
                        error.get("retry_after"),
                        retryable=error.get("retryable"),
                        action=error.get("action"),
                        details=error.get("details"),
                        status=exc.status_code,
                    )["error"]
                    return JSONResponse(status_code=exc.status_code, content=content)
                code = _STATUS_CODES.get(exc.status_code, "invalid_request")
                message = str(exc.detail) if exc.detail else code
                return JSONResponse(
                    status_code=exc.status_code,
                    content=error_body(code, message, status=exc.status_code),
                    headers=exc.headers,
                )

        return handler
