"""Identity surface: auth, /me, api-keys, workspaces (RFC 08 §1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from control.api.deps import principal_dep, require_workspace
from control.api.errors import ApiError
from control.api.views import api_key_view, user_view, workspace_view
from control.domain.errors import DomainError
from control.persistence.unit_of_work import SqlUnitOfWork

router = APIRouter(prefix="/api")


def _user_view(uow: SqlUnitOfWork, user_id: str) -> dict:
    user = uow.users.get(user_id)
    if user is None:
        raise DomainError("not_found", "user not found")
    return user_view(user, uow.workspaces.memberships_of(user_id))


@router.post("/auth/register", status_code=201)
def register(request: Request, body: dict) -> dict:
    svc = request.app.state.auth
    with SqlUnitOfWork(request.app.state.db) as uow:
        result = svc.signup(
            uow,
            email=body.get("email", ""),
            password=body.get("password", ""),
            display_name=body.get("display_name"),
        )
    # The verification token is delivered out-of-band (email); never in the
    # response body.
    return {
        "user_id": result.user_id,
        "workspace_id": result.workspace_id,
        "email_verification_required": True,
    }


@router.post("/auth/login")
def login(request: Request, body: dict) -> Response:
    svc = request.app.state.auth
    with SqlUnitOfWork(request.app.state.db) as uow:
        result = svc.login(
            uow,
            email=body.get("email", ""),
            password=body.get("password", ""),
            client_key=request.client.host if request.client else None,
        )
        view = _user_view(uow, result.principal.user_id)
    resp = JSONResponse(
        {
            "token": result.token,
            "user": view,
            "login_session_id": result.login_session_id,
        }
    )
    resp.set_cookie(
        "sbx_session",
        result.token,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
    )
    return resp


@router.post("/auth/logout")
def logout(request: Request, principal: dict = Depends(principal_dep)) -> Response:
    svc = request.app.state.auth
    login_id = getattr(request.state, "login_session_id", None)
    if login_id:
        with SqlUnitOfWork(request.app.state.db) as uow:
            svc.logout(uow, login_session_id=login_id)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("sbx_session")
    return resp


@router.post("/auth/email-verifications")
def verify_email(request: Request, body: dict) -> dict:
    svc = request.app.state.auth
    with SqlUnitOfWork(request.app.state.db) as uow:
        return svc.verify_email(uow, token=body.get("token", ""))


@router.post("/auth/password-resets", status_code=202)
def request_reset(request: Request, body: dict) -> dict:
    svc = request.app.state.auth
    with SqlUnitOfWork(request.app.state.db) as uow:
        svc.request_password_reset(uow, email=body.get("email", ""))
    # Deliberately uniform — no account-existence leak.
    return {"accepted": True}


@router.post("/auth/password-changes")
def reset_password(request: Request, body: dict) -> dict:
    svc = request.app.state.auth
    with SqlUnitOfWork(request.app.state.db) as uow:
        svc.reset_password(uow, token=body.get("token", ""), new_password=body.get("password", ""))
        return {"ok": True}


@router.get("/me")
def me(request: Request, principal: dict = Depends(principal_dep)) -> dict:
    p = request.state.principal_obj
    with SqlUnitOfWork(request.app.state.db) as uow:
        view = _user_view(uow, p.user_id)
    view["auth"] = {
        "label": p.label,
        "scopes": sorted(p.scopes),
        "auth_epoch": p.auth_epoch,
    }
    return view


@router.get("/api-keys")
def list_api_keys(request: Request, principal: dict = Depends(principal_dep)) -> dict:
    p = request.state.principal_obj
    with SqlUnitOfWork(request.app.state.db) as uow:
        rows = uow.rows.all(
            "SELECT * FROM api_keys WHERE user_id=%s AND revoked_at IS NULL",
            (p.user_id,),
        )
    return {"items": [api_key_view(k) for k in rows]}


@router.post("/api-keys", status_code=201)
def create_api_key(request: Request, body: dict, principal: dict = Depends(principal_dep)) -> dict:
    p = request.state.principal_obj
    svc = request.app.state.auth
    with SqlUnitOfWork(request.app.state.db) as uow:
        result = svc.create_api_key(
            uow,
            user_id=p.user_id,
            label=body.get("label"),
            scopes=body.get("scopes") or ["*"],
        )
    return {
        "id": result.api_key_id,
        "key": result.plaintext,  # shown once, never stored or re-readable
        "scopes": result.scopes,
    }


@router.delete("/api-keys/{key_id}")
def revoke_api_key(request: Request, key_id: str, principal: dict = Depends(principal_dep)) -> dict:
    p = request.state.principal_obj
    svc = request.app.state.auth
    with SqlUnitOfWork(request.app.state.db) as uow:
        svc.revoke_api_key(uow, user_id=p.user_id, api_key_id=key_id)
    return {"revoked": True}


@router.get("/workspaces")
def list_workspaces(request: Request, principal: dict = Depends(principal_dep)) -> dict:
    p = request.state.principal_obj
    with SqlUnitOfWork(request.app.state.db) as uow:
        out = []
        for m in uow.workspaces.memberships_of(p.user_id):
            out.append(workspace_view(m, role=uow.workspaces.member_role(m["id"], p.user_id)))
    return {"items": out}


@router.get("/workspaces/{workspace_id}")
def get_workspace(
    request: Request, workspace_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    require_workspace(request, workspace_id)
    with SqlUnitOfWork(request.app.state.db) as uow:
        w = uow.workspaces.get(workspace_id)
        if w is None:
            raise ApiError(
                404, code="not_found", category="resource", message="workspace not found"
            )
        role = uow.workspaces.member_role(workspace_id, request.state.principal_obj.user_id)
        return workspace_view(w, role=role)
