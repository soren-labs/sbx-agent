"""Project application: immutable ProjectVersions under a mutable
current-pointer Project (RFC 167 §02). Publishing a version and moving the
pointer are separate effects; the pointer update is CASed on the project
version."""

from __future__ import annotations

import re

from control.domain.errors import DomainError
from control.domain.ids import new_id
from control.domain.projects import (
    EnvironmentSpec,
    ExecutionDefaults,
    ProjectVersion,
    ServiceDeclaration,
)
from control.persistence.unit_of_work import SqlUnitOfWork

_slug_re = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}$")


class ProjectService:
    def __init__(self, db):
        self.db = db

    def create_project(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        slug: str,
        name: str,
        metadata: dict | None = None,
    ) -> dict:
        if not _slug_re.match(slug):
            raise DomainError("validation_failed", f"invalid project slug {slug!r}")
        if uow.projects.get_by_slug(workspace_id, slug) is not None:
            raise DomainError("idempotency_conflict", f"project slug {slug!r} taken")
        project_id = new_id("project")
        uow.projects.insert(
            {
                "id": project_id,
                "workspace_id": workspace_id,
                "slug": slug,
                "name": name,
                "current_version_id": None,
                "metadata": metadata or {},
            }
        )
        uow.commit()
        return uow.projects.get(workspace_id, project_id)

    def publish_version(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        project_id: str,
        repository: str | None,
        base_ref: str = "main",
        environment: dict | None = None,
        services: list[dict] | None = None,
        defaults: dict | None = None,
        ship_policy: dict | None = None,
        created_by: str | None = None,
        set_current: bool = True,
    ) -> dict:
        project = uow.projects.get(workspace_id, project_id)
        if project is None:
            raise DomainError("not_found", "project not found")
        if repository is not None and not re.match(r"^[\w.\-]+/[\w.\-]+$", repository):
            raise DomainError("validation_failed", f"invalid repository identity {repository!r}")

        ordinal = uow.rows.one(
            "SELECT COALESCE(MAX(ordinal), 0) + 1 AS n FROM project_versions WHERE project_id=%s",
            (project_id,),
        )["n"]
        pv = ProjectVersion(
            id=new_id("project_version"),
            workspace_id=workspace_id,
            project_id=project_id,
            ordinal=ordinal,
            repository=repository,
            base_ref=base_ref,
            environment=EnvironmentSpec(**(environment or {})),
            services=tuple(
                ServiceDeclaration(**{**s, "argv": tuple(s.get("argv") or ())})
                for s in (services or [])
            ),
            defaults=ExecutionDefaults(**(defaults or {})),
            spec_digest=None,
            created_by=created_by,
        )
        pv.spec_digest = pv.compute_digest()
        uow.project_versions.insert(
            {
                "id": pv.id,
                "workspace_id": workspace_id,
                "project_id": project_id,
                "ordinal": ordinal,
                "repository": repository,
                "base_ref": base_ref,
                "environment": pv.environment.to_dict(),
                "services": [s.to_dict() for s in pv.services],
                "defaults": pv.defaults.to_dict(),
                "ship_policy": ship_policy or {},
                "spec_digest": pv.spec_digest,
                "created_by": created_by,
            }
        )
        if set_current:
            uow.conn.execute(
                "UPDATE projects SET current_version_id=%s, version=version+1,"
                " updated_at=now() WHERE id=%s AND version=%s",
                (pv.id, project_id, project["version"]),
            )
        uow.commit()
        return uow.project_versions.get(workspace_id, pv.id)

    def get_project(
        self, uow: SqlUnitOfWork, *, workspace_id: str, project_id_or_slug: str
    ) -> dict:
        row = uow.projects.get(workspace_id, project_id_or_slug) or uow.projects.get_by_slug(
            workspace_id, project_id_or_slug
        )
        if row is None:
            raise DomainError("not_found", "project not found")
        return row

    def get_version(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        project_version_id: str,
    ) -> dict:
        row = uow.project_versions.get(workspace_id, project_version_id)
        if row is None:
            raise DomainError("not_found", "project version not found")
        return row

    def current_version(
        self, uow: SqlUnitOfWork, *, workspace_id: str, project_id: str
    ) -> dict | None:
        project = self.get_project(uow, workspace_id=workspace_id, project_id_or_slug=project_id)
        if not project["current_version_id"]:
            return None
        return self.get_version(
            uow, workspace_id=workspace_id, project_version_id=project["current_version_id"]
        )

    def list_projects(self, uow: SqlUnitOfWork, *, workspace_id: str) -> list[dict]:
        return uow.projects.list(workspace_id)


def projectless_spec(
    *,
    repository: str | None = None,
    executor_backend: str = "local",
    environment: dict | None = None,
    model: str | None = None,
    provider_id: str | None = None,
) -> dict:
    """Session-level spec for projectless work (same worktree/runtime model)."""
    return {
        "repository": repository,
        "executor_backend": executor_backend,
        "environment": environment or {},
        "model": model,
        "provider_id": provider_id,
    }
