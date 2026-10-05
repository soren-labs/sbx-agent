"""Request authentication + scoping for the unified /api.

Three credential channels, one Principal:
- ``Authorization: Bearer sbx_k_...`` → API key (hash-indexed, auth_epoch bound)
- ``Authorization: Bearer <opaque>`` or ``sbx_session`` cookie → login session
- anything else → 401, never ambient fallback

No route treats possession of an ID as access: detail endpoints always
re-check workspace membership.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import Request

from control.api.errors import ApiError
from control.application.auth import _hash_token
from control.domain.identity import Principal
from control.persistence.unit_of_work import SqlUnitOfWork


def _unauthenticated(msg: str = "authentication required") -> ApiError:
    return ApiError(401, code="unauthenticated", category="authentication", message=msg)


def resolve_principal(request: Request) -> tuple[Principal, str | None]:
    """Bearer API key, Bearer/cookie login token → Principal. Returns the
    login_session_id when a session token was used (for logout)."""
    auth: str | None = request.headers.get("authorization")
    cookie: str | None = request.cookies.get("sbx_session")
    svc = request.app.state.auth
    db = request.app.state.db

    token: str | None = None
    if auth and auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token.startswith("sbx_k_"):
            with SqlUnitOfWork(db) as uow:
                return svc.authenticate_api_key(uow, key=token), None
    if token is None:
        token = cookie
    if not token:
        raise _unauthenticated()
    with SqlUnitOfWork(db) as uow:
        row = uow.rows.one(
            "SELECT id FROM login_sessions WHERE token_hash=%s"
            " AND revoked_at IS NULL AND expires_at > now()",
            (_hash_token(token),),
        )
        principal = svc.authenticate_cookie(uow, token=token)
    return principal, (row["id"] if row else None)


async def principal_dep(request: Request) -> dict:
    """FastAPI dependency → dict principal used by service calls."""
    principal, login_id = resolve_principal(request)
    request.state.principal_obj = principal
    request.state.login_session_id = login_id
    return {"kind": "user", "id": principal.user_id}


def require_workspace(request: Request, workspace_id: str) -> None:
    """Owner-scope check on a workspace path parameter."""
    principal: Principal = request.state.principal_obj
    if workspace_id not in principal.workspace_ids:
        raise ApiError(
            404,
            code="not_found",
            category="resource",
            message="workspace not found",
        )


def _request_hash(method: str, path: str, body: Any) -> str:
    blob = json.dumps(
        {"method": method, "path": path, "body": body},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def idem_replay(
    uow: SqlUnitOfWork,
    request: Request,
    *,
    workspace_id: str,
    body: Any,
) -> dict | None:
    """Return the recorded response for an identical committed request,
    or raise idempotency_conflict on a changed payload under a reused key.
    ``None`` means this key is new — the caller proceeds and then records
    via :func:`idem_record` in the SAME transaction as the mutation."""
    key = request.headers.get("idempotency-key")
    if not key:
        raise ApiError(
            422,
            code="validation_failed",
            category="request",
            message="Idempotency-Key header is required on mutations",
        )
    if len(key) > 200:
        raise ApiError(
            422,
            code="validation_failed",
            category="request",
            message="Idempotency-Key too long",
        )
    row = uow.rows.one(
        "SELECT * FROM api_idempotency_keys WHERE workspace_id=%s AND key=%s",
        (workspace_id, key),
    )
    if row is None:
        request.state.idem_key = key
        return None
    if row["request_hash"] != _request_hash(request.method, request.url.path, body):
        raise ApiError(
            409,
            code="idempotency_conflict",
            category="concurrency",
            message="Idempotency-Key reused with a different payload",
        )
    return row


def idem_record(
    uow: SqlUnitOfWork,
    request: Request,
    *,
    workspace_id: str,
    body: Any,
    status: int,
    response: dict,
    resource_ids: dict | None = None,
) -> None:
    key = getattr(request.state, "idem_key", None)
    if not key:
        return
    uow.rows.one(
        "INSERT INTO api_idempotency_keys"
        " (workspace_id, key, method, path, request_hash, status, response,"
        "  resource_ids)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)"
        " ON CONFLICT (workspace_id, key) DO NOTHING RETURNING key",
        (
            workspace_id,
            key,
            request.method,
            request.url.path,
            _request_hash(request.method, request.url.path, body),
            status,
            json.dumps(response),
            json.dumps(resource_ids or {}),
        ),
    )


def pagination(
    *,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[int, int]:
    """Offset-backed bounded paging; cursor is the opaque offset."""
    if limit < 1 or limit > 200:
        raise ApiError(
            422,
            code="validation_failed",
            category="request",
            message="limit must be 1..200",
        )
    offset = 0
    if cursor:
        try:
            offset = int(cursor)
        except (TypeError, ValueError) as exc:
            raise ApiError(
                422,
                code="invalid_cursor",
                category="request",
                message="cursor is not a valid page token",
            ) from exc
        if offset < 0:
            raise ApiError(
                422,
                code="invalid_cursor",
                category="request",
                message="cursor is not a valid page token",
            )
    return limit, offset


def require_idem(request: Request) -> None:
    if not request.headers.get("idempotency-key"):
        raise ApiError(
            422,
            code="validation_failed",
            category="request",
            message="Idempotency-Key header is required on mutations",
        )


__all__ = [
    "principal_dep",
    "require_workspace",
    "idem_replay",
    "idem_record",
    "require_idem",
    "pagination",
]
