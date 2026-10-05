"""Projects, connections, catalog surfaces (RFC 08 §3–§5)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from control.api import views
from control.api.deps import (
    idem_record,
    idem_replay,
    pagination,
    principal_dep,
    require_idem,
    require_workspace,
)
from control.api.errors import ApiError
from control.application.sessions import enqueue_job
from control.domain.jobs import JobKind, TargetFamily
from control.persistence.unit_of_work import SqlUnitOfWork

router = APIRouter(prefix="/api")


def _nf(what: str) -> ApiError:
    return ApiError(404, code="not_found", category="resource", message=f"{what} not found")


def _get_project(request: Request, uow: SqlUnitOfWork, project_id: str) -> dict:
    for ws in request.state.principal_obj.workspace_ids:
        row = uow.projects.get(ws, project_id) or uow.projects.get_by_slug(ws, project_id)
        if row is not None:
            return row
    raise _nf("project")


def _owner_row(request: Request, row: dict | None, what: str) -> dict:
    if row is None:
        raise _nf(what)
    if row.get("workspace_id") not in request.state.principal_obj.workspace_ids:
        raise _nf(what)
    return row


# ---- projects -------------------------------------------------------------


@router.get("/workspaces/{workspace_id}/projects")
def list_projects(
    request: Request,
    workspace_id: str,
    principal: dict = Depends(principal_dep),
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    require_workspace(request, workspace_id)
    lim, off = pagination(limit=limit, cursor=cursor)
    svc = request.app.state.projects
    with SqlUnitOfWork(request.app.state.db) as uow:
        rows = svc.list_projects(uow, workspace_id=workspace_id)
    items = rows[off : off + lim]
    out = {"items": [views.project_view(p) for p in items]}
    if off + lim < len(rows):
        out["next_cursor"] = str(off + lim)
    return out


@router.post("/workspaces/{workspace_id}/projects", status_code=201)
def create_project(
    request: Request,
    workspace_id: str,
    body: dict,
    principal: dict = Depends(principal_dep),
) -> dict:
    require_workspace(request, workspace_id)
    require_idem(request)
    svc = request.app.state.projects
    with SqlUnitOfWork(request.app.state.db) as uow:
        replay = idem_replay(uow, request, workspace_id=workspace_id, body=body)
        if replay is not None:
            return replay["response"]
        row = svc.create_project(
            uow,
            workspace_id=workspace_id,
            slug=body.get("slug", ""),
            name=body.get("name", ""),
            metadata=body.get("metadata"),
        )
        resp = views.project_view(row)
        idem_record(
            uow,
            request,
            workspace_id=workspace_id,
            body=body,
            status=201,
            response=resp,
            resource_ids={"project_id": row["id"]},
        )
        uow.commit()
        return resp


@router.get("/projects/{project_id}")
def get_project(
    request: Request, project_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        return views.project_view(_get_project(request, uow, project_id))


@router.post("/projects/{project_id}/versions", status_code=201)
def publish_version(
    request: Request,
    project_id: str,
    body: dict,
    principal: dict = Depends(principal_dep),
) -> dict:
    require_idem(request)
    svc = request.app.state.projects
    with SqlUnitOfWork(request.app.state.db) as uow:
        p = _get_project(request, uow, project_id)
        replay = idem_replay(uow, request, workspace_id=p["workspace_id"], body=body)
        if replay is not None:
            return replay["response"]
        row = svc.publish_version(
            uow,
            workspace_id=p["workspace_id"],
            project_id=project_id,
            repository=body.get("repository"),
            base_ref=body.get("base_ref", "main"),
            environment=body.get("environment"),
            services=body.get("services"),
            defaults=body.get("defaults"),
            ship_policy=body.get("ship_policy"),
            created_by=principal["id"],
        )
        resp = views.project_version_view(row)
        idem_record(
            uow,
            request,
            workspace_id=p["workspace_id"],
            body=body,
            status=201,
            response=resp,
            resource_ids={"project_version_id": row["id"]},
        )
        uow.commit()
        return resp


@router.get("/projects/{project_id}/versions")
def list_versions(
    request: Request, project_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        p = _get_project(request, uow, project_id)
        rows = uow.rows.all(
            "SELECT * FROM project_versions WHERE project_id=%s ORDER BY ordinal",
            (p["id"],),
        )
    return {"items": [views.project_version_view(v) for v in rows]}


@router.get("/project-versions/{version_id}")
def get_version(
    request: Request, version_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        v = uow.rows.one("SELECT * FROM project_versions WHERE id=%s", (version_id,))
        return views.project_version_view(_owner_row(request, v, "project_version"))


# ---- connections ----------------------------------------------------------


@router.get("/workspaces/{workspace_id}/connections")
def list_connections(
    request: Request,
    workspace_id: str,
    principal: dict = Depends(principal_dep),
    kind: str | None = None,
    state: str | None = None,
) -> dict:
    require_workspace(request, workspace_id)
    svc = request.app.state.connections
    with SqlUnitOfWork(request.app.state.db) as uow:
        rows = svc.list_connections(uow, workspace_id=workspace_id)
    items = [
        views.connection_view(c)
        for c in rows
        if (kind is None or c["kind"] == kind) and (state is None or c["state"] == state)
    ]
    return {"items": items}


@router.post("/workspaces/{workspace_id}/connections", status_code=201)
def create_connection(
    request: Request,
    workspace_id: str,
    body: dict,
    principal: dict = Depends(principal_dep),
) -> dict:
    require_workspace(request, workspace_id)
    require_idem(request)
    svc = request.app.state.connections
    with SqlUnitOfWork(request.app.state.db) as uow:
        replay = idem_replay(uow, request, workspace_id=workspace_id, body=body)
        if replay is not None:
            return replay["response"]
        row = svc.create_connection(
            uow,
            workspace_id=workspace_id,
            principal_id=principal["id"],
            kind=body.get("kind", ""),
            label=body.get("label"),
            credential=body.get("credential") or {},
            acquisition=body.get("acquisition", "manual"),
            allowed_purposes=body.get("allowed_purposes"),
        )
        resp = views.connection_view(row)
        idem_record(
            uow,
            request,
            workspace_id=workspace_id,
            body=body,
            status=201,
            response=resp,
            resource_ids={"connection_id": row["id"]},
        )
        uow.commit()
        return resp


def _get_connection(request: Request, uow: SqlUnitOfWork, connection_id: str) -> dict:
    for ws in request.state.principal_obj.workspace_ids:
        row = uow.connections.get(ws, connection_id)
        if row is not None:
            return row
    raise _nf("connection")


@router.get("/connections/{connection_id}")
def get_connection(
    request: Request, connection_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        row = _get_connection(request, uow, connection_id)
        return views.connection_view(row)


@router.patch("/connections/{connection_id}")
def patch_connection(
    request: Request,
    connection_id: str,
    body: dict,
    principal: dict = Depends(principal_dep),
) -> dict:
    svc = request.app.state.connections
    with SqlUnitOfWork(request.app.state.db) as uow:
        row = _get_connection(request, uow, connection_id)
        ws = row["workspace_id"]
        if body.get("expected_version") is not None and (
            body["expected_version"] != row.get("version")
        ):
            raise ApiError(
                409,
                code="version_conflict",
                category="concurrency",
                message="connection has changed since your read",
            )
        if body.get("state") == "disabled":
            row = svc.disable(uow, workspace_id=ws, connection_id=connection_id)
        elif body.get("state") == "configured":
            row = svc.enable(uow, workspace_id=ws, connection_id=connection_id)
        elif body.get("label") is not None:
            uow.connections.update(ws, connection_id, {"label": body["label"]})
            row = uow.connections.get(ws, connection_id)
        return views.connection_view(row)


@router.post("/connections/{connection_id}/credential-versions", status_code=201)
def replace_credential(
    request: Request,
    connection_id: str,
    body: dict,
    principal: dict = Depends(principal_dep),
) -> dict:
    require_idem(request)
    svc = request.app.state.connections
    with SqlUnitOfWork(request.app.state.db) as uow:
        row = _get_connection(request, uow, connection_id)
        ws = row["workspace_id"]
        replay = idem_replay(uow, request, workspace_id=ws, body=body)
        if replay is not None:
            return replay["response"]
        if body.get("expected_version") is not None and (
            body["expected_version"] != row.get("version")
        ):
            raise ApiError(
                409,
                code="version_conflict",
                category="concurrency",
                message="connection has changed since your read",
            )
        out = svc.replace_credential(
            uow,
            workspace_id=ws,
            connection_id=connection_id,
            credential=body.get("credential") or {},
            principal_id=principal["id"],
        )
        resp = {
            "connection": views.connection_view(out),
            "credential_version_id": out.get("current_credential_version_id"),
            "validation_job_id": out.get("validation_job_id"),
        }
        idem_record(
            uow,
            request,
            workspace_id=ws,
            body=body,
            status=201,
            response=resp,
            resource_ids={"connection_id": connection_id},
        )
        uow.commit()
        return resp


@router.post("/connections/{connection_id}/validations", status_code=202)
def validate_connection(
    request: Request,
    connection_id: str,
    body: dict | None = None,
    principal: dict = Depends(principal_dep),
) -> dict:
    """Durable connector validation — a Job, not a synchronous probe."""
    with SqlUnitOfWork(request.app.state.db) as uow:
        row = _get_connection(request, uow, connection_id)
        dedupe = f"connection.validate:{connection_id}"
        job = enqueue_job(
            uow,
            workspace_id=row["workspace_id"],
            kind=JobKind.CONNECTION_VALIDATE,
            dedupe_key=dedupe,
            payload={"connection_id": connection_id},
            target_family=TargetFamily.CONNECTION,
            target_id=connection_id,
        )
        uow.commit()
        return {
            "accepted": True,
            "connection_id": connection_id,
            "job": views.job_view(job),
        }


@router.get("/connections/{connection_id}/capabilities")
def connection_capabilities(
    request: Request, connection_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    with SqlUnitOfWork(request.app.state.db) as uow:
        row = _get_connection(request, uow, connection_id)
        obs = uow.rows.all(
            "SELECT * FROM connection_observations WHERE connection_id=%s"
            " ORDER BY observed_at DESC LIMIT 20",
            (connection_id,),
        )
    return {
        "connection_id": connection_id,
        "state": row["state"],
        "health": row["health"],
        "external_identity": row.get("external_identity") or {},
        "capability_observations": row.get("capability_observations") or {},
        "observations": [
            {
                "capability": o.get("capability"),
                "state": o.get("state"),
                "detail": o.get("detail") or {},
                "observed_at": views._ts(o, "observed_at"),
            }
            for o in obs
        ],
    }


@router.delete("/connections/{connection_id}")
def disconnect(
    request: Request, connection_id: str, principal: dict = Depends(principal_dep)
) -> dict:
    svc = request.app.state.connections
    with SqlUnitOfWork(request.app.state.db) as uow:
        row = _get_connection(request, uow, connection_id)
        out = svc.disconnect(
            uow,
            workspace_id=row["workspace_id"],
            connection_id=connection_id,
            principal_id=principal["id"],
        )
        uow.commit()
        return {"connection": views.connection_view(out)}


# ---- catalog: models / harnesses / backends --------------------------------


@router.get("/models")
def list_models(
    request: Request,
    workspace_id: str,
    principal: dict = Depends(principal_dep),
) -> dict:
    require_workspace(request, workspace_id)
    svc = request.app.state.models
    with SqlUnitOfWork(request.app.state.db) as uow:
        models = svc.list_models(uow, workspace_id=workspace_id)
        try:
            default = svc.pick_default(uow, workspace_id=workspace_id)
        except Exception:
            default = None
    return {
        "items": [views.model_view(m) for m in models],
        "default_model": views.model_view(default) if default else None,
    }


@router.get("/harnesses")
def list_harnesses(request: Request) -> dict:
    """Truthful capability catalog — providers with a wired harness."""
    return {
        "items": [
            {
                "provider_id": "opencode",
                "official_cli": "opencode",
                "connection_kinds": ["opencode_zen"],
                "capabilities": {
                    "turns": True,
                    "resume": True,
                    "native_history": True,
                    "cancel": True,
                },
            }
        ]
    }


@router.get("/executor-backends")
def list_backends(request: Request) -> dict:
    return {
        "items": [
            {
                "backend": "modal",
                "connection_kind": "modal",
                "worktree_filesystem": True,
                "status": "available",
            },
            {
                "backend": "local",
                "connection_kind": None,
                "worktree_filesystem": True,
                "status": "available",
            },
        ]
    }
