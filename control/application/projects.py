"""Project application: immutable ProjectVersions and a CAS current pointer (RFC 02)."""

from __future__ import annotations

import re
from typing import Any

from control.application import access
from control.domain.digests import digest_of
from control.domain.errors import DomainError
from control.domain.identity import Principal
from control.domain.ids import new_id
from control.security.redaction import find_secret_fields

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DEFAULT_SHIP_POLICY = {
    "transport": "pull_request",
    "base_branch": None,
    "draft": True,
    "automatic_delivery": False,
    "required_results": [{"kind": "ReviewAssessment", "count": 1, "verdict": "approve"}],
    "required_checks": [],
    "independence": "distinct_child_session",
    "merge_methods": ["squash"],
    "require_base_unchanged": False,
}


def _argv(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(a, str) and a for a in value):
        raise DomainError(
            "validation_failed", f"{field} must be a non-empty argv list", details={"field": field}
        )
    return list(value)


def normalize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise DomainError("validation_failed", "spec must be an object")
    repo = spec.get("repository")
    repository = None
    if repo:
        full = str(repo.get("full_name") or "")
        if not _REPO.match(full):
            raise DomainError(
                "validation_failed",
                "repository.full_name must be owner/name",
                details={"field": "repository.full_name"},
            )
        repository = {
            "provider": repo.get("provider") or "github",
            "full_name": full,
            "base_ref": str(repo.get("base_ref") or "main"),
            "clone_url": f"https://github.com/{full}.git",
        }
    env = (spec.get("environment") or {}).get("env") or {}
    if (
        not isinstance(env, dict)
        or find_secret_fields(env)
        or any(not isinstance(v, str) for v in env.values())
    ):
        raise DomainError(
            "validation_failed",
            "environment.env holds non-secret string values only; bind secrets through Connections",
            details={"field": "environment.env"},
        )
    checks = []
    for i, check in enumerate(spec.get("checks") or []):
        checks.append(
            {
                "name": str(check.get("name") or f"check-{i}")[:60],
                "argv": _argv(check.get("argv"), f"checks[{i}].argv"),
                "timeout_seconds": int(check.get("timeout_seconds") or 600),
            }
        )
    services = []
    for i, svc in enumerate(spec.get("services") or []):
        services.append(
            {
                "name": str(svc.get("name") or f"service-{i}")[:60],
                "argv": _argv(svc.get("argv"), f"services[{i}].argv"),
                "cwd": str(svc.get("cwd") or "."),
                "port": int(svc["port"]) if svc.get("port") else None,
                "health": svc.get("health") or None,
                "restart": svc.get("restart") or "on-failure",
                "order": int(svc.get("order") or i),
                "preview": bool(svc.get("preview")),
            }
        )
    defaults = spec.get("defaults") or {}
    ship = {**DEFAULT_SHIP_POLICY, **(spec.get("ship_policy") or {})}
    if ship["base_branch"] is None and repository:
        ship["base_branch"] = repository["base_ref"]
    if ship["transport"] not in ("export", "git_branch", "pull_request", "direct_base"):
        raise DomainError(
            "validation_failed",
            "unknown ship transport",
            details={"field": "ship_policy.transport"},
        )
    return {
        "schema_version": 1,
        "repository": repository,
        "environment": {"env": env},
        "checks": checks,
        "services": services,
        "defaults": {
            "harness": defaults.get("harness") or {"provider_id": "opencode"},
            "executor": defaults.get("executor")
            or {"backend": "modal", "resource_class": "standard"},
            "turn_defaults": defaults.get("turn_defaults") or {},
        },
        "ship_policy": ship,
    }


def project_view(project: dict[str, Any], version: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": project["id"],
        "workspace_id": project["workspace_id"],
        "slug": project["slug"],
        "name": project["name"],
        "version": project["version"],
        "current_version": version_view(version) if version else None,
        "created_at": project["created_at"].isoformat(),
        "updated_at": project["updated_at"].isoformat(),
    }


def version_view(v: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": v["id"],
        "project_id": v["project_id"],
        "ordinal": v["ordinal"],
        "spec": v["spec"],
        "spec_digest": v["spec_digest"],
        "created_at": v["created_at"].isoformat(),
    }


class Projects:
    def __init__(self, tx: Any) -> None:
        self.tx = tx

    def create(
        self,
        principal: Principal,
        workspace_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        access.require_workspace(principal, workspace_id)
        slug = str(body.get("slug") or "").strip()
        if not _SLUG.match(slug):
            raise DomainError(
                "validation_failed",
                "slug must be lowercase letters, digits and dashes",
                details={"field": "slug"},
            )
        spec = normalize_spec(body.get("spec") or {})

        def fn(uow: Any) -> dict[str, Any]:
            replay = uow.dedupe_begin(
                principal_id=principal.user_id,
                workspace_id=workspace_id,
                command_kind="projects.create",
                key=idempotency_key,
                request=body,
            )
            if replay is not None:
                return replay
            if uow.find_one("projects", {"workspace_id": workspace_id, "slug": slug}):
                raise DomainError(
                    "version_conflict",
                    "a project with this slug already exists",
                    details={"field": "slug"},
                )
            project = uow.insert(
                "projects",
                {
                    "id": new_id("project"),
                    "workspace_id": workspace_id,
                    "slug": slug,
                    "name": str(body.get("name") or slug)[:120],
                    "created_by": principal.user_id,
                },
            )
            version = self._insert_version(uow, principal, project, spec)
            project = uow.update("projects", project["id"], {"current_version_id": version["id"]})
            response = project_view(project, version)
            uow.dedupe_finish(
                principal_id=principal.user_id,
                workspace_id=workspace_id,
                command_kind="projects.create",
                key=idempotency_key,
                response=response,
            )
            return response

        return self.tx.run(fn)

    def _insert_version(
        self, uow: Any, principal: Principal, project: dict[str, Any], spec: dict[str, Any]
    ) -> dict[str, Any]:
        ordinal = uow.count("project_versions", {"project_id": project["id"]}) + 1
        repo = spec["repository"] or {}
        full = repo.get("full_name", "/").split("/")
        return uow.insert(
            "project_versions",
            {
                "id": new_id("project_version"),
                "workspace_id": project["workspace_id"],
                "project_id": project["id"],
                "ordinal": ordinal,
                "repo_provider": repo.get("provider"),
                "repo_owner": full[0] or None,
                "repo_name": full[1] if len(full) > 1 else None,
                "base_ref": repo.get("base_ref"),
                "spec": spec,
                "spec_digest": digest_of(spec),
                "created_by": principal.user_id,
            },
        )

    def publish_version(
        self,
        principal: Principal,
        project_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        spec = normalize_spec(body.get("spec") or {})
        expected = body.get("expected_version")

        def fn(uow: Any) -> dict[str, Any]:
            project = access.owned(
                uow, principal, "projects", project_id, lock=True, what="project"
            )
            replay = uow.dedupe_begin(
                principal_id=principal.user_id,
                workspace_id=project["workspace_id"],
                command_kind=f"projects.versions:{project_id}",
                key=idempotency_key,
                request=body,
            )
            if replay is not None:
                return replay
            if expected is None or int(expected) != project["version"]:
                raise DomainError(
                    "version_conflict",
                    "project changed; reload and retry",
                    details={"current_version": project["version"]},
                )
            version = self._insert_version(uow, principal, project, spec)
            project = uow.update(
                "projects",
                project_id,
                {"current_version_id": version["id"], "updated_at": uow.now()},
                bump_version=True,
            )
            response = project_view(project, version)
            uow.dedupe_finish(
                principal_id=principal.user_id,
                workspace_id=project["workspace_id"],
                command_kind=f"projects.versions:{project_id}",
                key=idempotency_key,
                response=response,
            )
            return response

        return self.tx.run(fn)

    def update(self, principal: Principal, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            project = access.owned(
                uow, principal, "projects", project_id, lock=True, what="project"
            )
            if (
                body.get("expected_version") is None
                or int(body["expected_version"]) != project["version"]
            ):
                raise DomainError(
                    "version_conflict",
                    "project changed",
                    details={"current_version": project["version"]},
                )
            project = uow.update(
                "projects",
                project_id,
                {"name": str(body.get("name") or project["name"])[:120], "updated_at": uow.now()},
                bump_version=True,
            )
            return project_view(project, uow.get("project_versions", project["current_version_id"]))

        return self.tx.run(fn)

    def get(self, principal: Principal, project_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            project = access.owned(uow, principal, "projects", project_id, what="project")
            return project_view(project, uow.get("project_versions", project["current_version_id"]))

        return self.tx.read(fn)

    def list(self, principal: Principal, workspace_id: str) -> dict[str, Any]:
        access.require_workspace(principal, workspace_id)

        def fn(uow: Any) -> dict[str, Any]:
            rows = uow.find("projects", {"workspace_id": workspace_id}, order="updated_at DESC")
            return {
                "items": [
                    project_view(p, uow.get("project_versions", p["current_version_id"]))
                    for p in rows
                ]
            }

        return self.tx.read(fn)

    def versions(self, principal: Principal, project_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            access.owned(uow, principal, "projects", project_id, what="project")
            return {
                "items": [
                    version_view(v)
                    for v in uow.find(
                        "project_versions", {"project_id": project_id}, order="ordinal DESC"
                    )
                ]
            }

        return self.tx.read(fn)
