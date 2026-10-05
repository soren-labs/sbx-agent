"""Principal resolution (cookie or API key -> same Principal), CSRF/origin, idempotency."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from control.domain.errors import DomainError
from control.domain.identity import Principal

SESSION_COOKIE = "sbx_session"
CSRF_COOKIE = "sbx_csrf"
CSRF_HEADER = "x-csrf-token"


def services(request: Request) -> Any:
    return request.app.state.services


def _resolve(request: Request) -> tuple[Principal, str | None]:
    svc = services(request)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        principal = svc.identity.resolve_api_key(auth.split(" ", 1)[1].strip())
        if principal is None:
            raise DomainError("unauthenticated", "invalid or revoked API key")
        return principal, None
    resolved = svc.identity.resolve_session(request.cookies.get(SESSION_COOKIE))
    if resolved is None:
        raise DomainError("unauthenticated", "sign in required")
    return resolved


def principal(request: Request) -> Principal:
    return _resolve(request)[0]


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    allowed = services(request).config.allowed_origins
    if origin and allowed and origin not in allowed:
        raise DomainError("csrf_failed", "origin not allowed")


def mutating_principal(request: Request) -> Principal:
    found, csrf_hash = _resolve(request)
    if found.via == "cookie":
        check_origin(request)
        svc = services(request)
        header = request.headers.get(CSRF_HEADER)
        if (
            not header
            or header != request.cookies.get(CSRF_COOKIE)
            or not svc.identity.csrf_ok(csrf_hash, header)
        ):
            raise DomainError("csrf_failed", "missing or invalid CSRF token")
    return found


def idempotency_key(request: Request, *, required: bool = True) -> str | None:
    key = request.headers.get("idempotency-key")
    if required and not key:
        raise DomainError(
            "validation_failed",
            "Idempotency-Key header is required for mutations",
            details={"header": "Idempotency-Key"},
        )
    if key and len(key) > 200:
        raise DomainError("validation_failed", "Idempotency-Key too long")
    return key


async def json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise DomainError("validation_failed", "request body must be a JSON object") from None
    if not isinstance(body, dict):
        raise DomainError("validation_failed", "request body must be a JSON object")
    return body
