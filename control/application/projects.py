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
    """An exec argv: the program is a non-empty string; later arguments may be empty.

    Commands are never run through a shell, so quoting is the client's job: the Console's
    ``name=command`` syntax produces exactly this list (see console/src/features/projects/argv.ts).
    """
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(a, str) for a in value)
        or not value[0]
        or any("\x00" in a for a in value)
    ):
        raise DomainError(
            "validation_failed",
            f"{field} must be an argv list whose first item names a program",
            details={"field": field},
        )
    return list(value)


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DomainError(
            "validation_failed", f"{field} must be an object", details={"field": field}
        )
    return value


def _extras(obj: dict[str, Any], known: frozenset[str], field: str) -> dict[str, Any]:
    """Fields this schema does not interpret are kept verbatim (never silently dropped).

    They have no effect until a control plane understands them, and they must not carry
    secrets: those are bound through Connections.
    """
    extra = {k: v for k, v in obj.items() if k not in known}
    found = find_secret_fields(extra)
    if found:
        raise DomainError(
            "validation_failed",
            "project specs hold no secrets; bind them through Connections",
            details={"field": f"{field}.{found[0]}" if field else found[0]},
        )
    return extra


_TOP = frozenset(
    {"schema_version", "repository", "environment", "checks", "services", "defaults", "ship_policy"}
)
_REPOSITORY = frozenset({"provider", "full_name", "base_ref", "clone_url"})
_ENVIRONMENT = frozenset({"env"})
_CHECK = frozenset({"name", "argv", "timeout_seconds"})
_SERVICE = frozenset({"name", "argv", "cwd", "port", "health", "restart", "order", "preview"})
_DEFAULTS = frozenset({"harness", "executor", "turn_defaults"})


def normalize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate a complete ProjectVersion spec (schema 1) into its stored form.

    A spec is a full replacement, so clients derive a new version from the current one. The
    stored form is a fixed point: ``normalize_spec(normalize_spec(x)) == normalize_spec(x)``,
    hence republishing an unchanged spec keeps every field and the same digest.
    """
    spec = _object(spec, "spec")
    if spec.get("schema_version", 1) != 1:
        raise DomainError(
            "validation_failed",
            "unsupported project spec schema_version",
            details={"field": "schema_version"},
        )
    repo = spec.get("repository")
    repository = None
    if repo:
        repo = _object(repo, "repository")
        full = str(repo.get("full_name") or "")
        if not _REPO.match(full):
            raise DomainError(
                "validation_failed",
                "repository.full_name must be owner/name",
                details={"field": "repository.full_name"},
            )
        repository = {
            **_extras(repo, _REPOSITORY, "repository"),
            "provider": repo.get("provider") or "github",
            "full_name": full,
            "base_ref": str(repo.get("base_ref") or "main"),
            "clone_url": f"https://github.com/{full}.git",
        }
    environment = _object(spec.get("environment") or {}, "environment")
    env = environment.get("env") or {}
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
    for i, raw in enumerate(spec.get("checks") or []):
        check = _object(raw, f"checks[{i}]")
        checks.append(
            {
                **_extras(check, _CHECK, f"checks[{i}]"),
                "name": str(check.get("name") or f"check-{i}")[:60],
                "argv": _argv(check.get("argv"), f"checks[{i}].argv"),
                "timeout_seconds": int(check.get("timeout_seconds") or 600),
            }
        )
    services = []
    for i, raw in enumerate(spec.get("services") or []):
        svc = _object(raw, f"services[{i}]")
        services.append(
            {
                **_extras(svc, _SERVICE, f"services[{i}]"),
                "name": str(svc.get("name") or f"service-{i}")[:60],
                "argv": _argv(svc.get("argv"), f"services[{i}].argv"),
                "cwd": str(svc.get("cwd") or "."),
                "port": int(svc["port"]) if svc.get("port") else None,
                "health": svc.get("health") or None,
                "restart": svc.get("restart") or "on-failure",
                "order": int(svc["order"]) if svc.get("order") is not None else i,
                "preview": bool(svc.get("preview")),
            }
        )
    names = [s["name"] for s in services]
    if len(set(names)) != len(names):
        raise DomainError(
            "validation_failed", "service names must be unique", details={"field": "services"}
        )
    defaults = _object(spec.get("defaults") or {}, "defaults")
    ship = {**DEFAULT_SHIP_POLICY, **_object(spec.get("ship_policy") or {}, "ship_policy")}
    if ship["base_branch"] is None and repository:
        ship["base_branch"] = repository["base_ref"]
    if ship["transport"] not in ("export", "git_branch", "pull_request", "direct_base"):
        raise DomainError(
            "validation_failed",
            "unknown ship transport",
            details={"field": "ship_policy.transport"},
        )
    return {
        **_extras(spec, _TOP, ""),
        "schema_version": 1,
        "repository": repository,
        "environment": {**_extras(environment, _ENVIRONMENT, "environment"), "env": env},
        "checks": checks,
        "services": services,
        "defaults": {
            **_extras(defaults, _DEFAULTS, "defaults"),
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
