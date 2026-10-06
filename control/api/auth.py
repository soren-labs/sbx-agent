"""/api/auth/*, /api/me, /api/api-keys (product identity)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

from control.api.dependencies import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    check_origin,
    json_body,
    mutating_principal,
    principal,
    services,
)
from control.domain.errors import DomainError

router = APIRouter()


@router.post("/api/auth/register", status_code=202)
async def register(request: Request) -> dict[str, Any]:
    check_origin(request)
    body = await json_body(request)
    return services(request).identity.register(body.get("email", ""), body.get("password", ""))


@router.post("/api/auth/email-verifications")
async def email_verification(request: Request) -> dict[str, Any]:
    body = await json_body(request)
    svc = services(request).identity
    if body.get("token"):
        return svc.verify_email(body["token"])
    return svc.request_verification(body.get("email", ""))


@router.post("/api/auth/login")
async def login(request: Request, response: Response) -> dict[str, Any]:
    check_origin(request)
    body = await json_body(request)
    svc = services(request)
    result = svc.identity.login(body.get("email", ""), body.get("password", ""))
    secure = svc.config.cookie_secure
    max_age = int(svc.identity.session_ttl.total_seconds())
    response.set_cookie(
        SESSION_COOKIE,
        result["session_token"],
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=max_age,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        result["csrf_token"],
        httponly=False,
        secure=secure,
        samesite="lax",
        max_age=max_age,
        path="/",
    )
    me = svc.identity.me(svc.identity.resolve_session(result["session_token"])[0])
    return {
        **me,
        "csrf_token": result["csrf_token"],
        "expires_at": result["expires_at"].isoformat(),
    }


@router.post("/api/auth/logout", status_code=204)
def logout(request: Request, response: Response) -> Response:
    mutating_principal(request)
    services(request).identity.logout(request.cookies.get(SESSION_COOKIE, ""))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    response.status_code = 204
    return response


@router.post("/api/auth/password-resets")
async def password_reset(request: Request) -> dict[str, Any]:
    body = await json_body(request)
    svc = services(request).identity
    if body.get("token"):
        return svc.reset_password(body["token"], body.get("new_password", ""))
    return svc.request_password_reset(body.get("email", ""))


@router.post("/api/auth/password-changes")
async def password_change(request: Request) -> dict[str, Any]:
    who = mutating_principal(request, scope="*")
    body = await json_body(request)
    return services(request).identity.change_password(
        who, body.get("current_password", ""), body.get("new_password", "")
    )


@router.get("/api/me")
def me(request: Request) -> dict[str, Any]:
    return services(request).identity.me(principal(request))


@router.get("/api/api-keys")
def list_keys(request: Request) -> dict[str, Any]:
    return services(request).identity.list_api_keys(principal(request))


@router.post("/api/api-keys", status_code=201)
async def create_key(request: Request) -> dict[str, Any]:
    who = mutating_principal(request, scope="*")
    return services(request).identity.create_api_key(who, await json_body(request))


@router.delete("/api/api-keys/{key_id}")
def revoke_key(request: Request, key_id: str) -> dict[str, Any]:
    return services(request).identity.revoke_api_key(mutating_principal(request, scope="*"), key_id)


@router.get("/api/workspaces")
def workspaces(request: Request) -> dict[str, Any]:
    return {"items": services(request).identity.me(principal(request))["workspaces"]}


@router.get("/api/workspaces/{workspace_id}")
def workspace(request: Request, workspace_id: str) -> dict[str, Any]:
    who = principal(request)
    if not who.owns(workspace_id):
        raise DomainError("not_found", "workspace not found")
    return services(request).identity.workspace(who, workspace_id)
