"""Project-aware Session input resolution with owner-scoped Connection selection."""

from __future__ import annotations

from typing import Any

from protocol.capabilities import INFERENCE_CONNECTION_KIND

from control.application.projects import DEFAULT_SHIP_POLICY
from control.application.resolution import ExplicitResolver
from control.domain.errors import DomainError, not_found
from control.domain.identity import Principal


class ProjectResolver(ExplicitResolver):
    def resolve(
        self, uow: Any, principal: Principal, workspace_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        defaults: dict[str, Any] = {}
        version_id = body.get("project_version_id")
        if body.get("project_id") and not version_id:
            project = uow.get("projects", body["project_id"], workspace_ids=[workspace_id])
            if project is None:
                raise not_found("project")
            version_id = project["current_version_id"]
        if version_id:
            version = uow.get("project_versions", version_id, workspace_ids=[workspace_id])
            if version is None:
                raise not_found("project version")
            spec = version["spec"]
            defaults = {
                "harness": spec["defaults"]["harness"],
                "executor": spec["defaults"]["executor"],
                "turn_defaults": spec["defaults"].get("turn_defaults") or {},
                "repository": spec.get("repository"),
                "environment": spec.get("environment"),
                "checks": spec.get("checks"),
                "services": spec.get("services"),
                "ship_policy": spec.get("ship_policy"),
                "project_id": version["project_id"],
                "project_version_id": version["id"],
            }
        if body.get("repository"):
            repo = dict(body["repository"])
            repo.setdefault("base_ref", "main")
            repo.setdefault("clone_url", f"https://github.com/{repo['full_name']}.git")
            body = {**body, "repository": repo}
        spec = self.base(uow, principal, workspace_id, body, defaults)
        spec["ship_policy"] = spec.get("ship_policy") or {
            **DEFAULT_SHIP_POLICY,
            "base_branch": (spec.get("repository") or {}).get("base_ref"),
        }
        spec["connections"] = self._select(uow, workspace_id, spec)
        if (
            not spec["harness"].get("model")
            and spec["harness"]["provider_id"] == "opencode"
            and spec["connections"].get("inference")
        ):
            spec["harness"]["model"] = self._preferred_model(uow, spec["connections"]["inference"])
        return spec

    def _select(self, uow: Any, workspace_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        wanted: dict[str, tuple[str | None, bool]] = {
            "inference": (INFERENCE_CONNECTION_KIND.get(spec["harness"]["provider_id"]), True),
            "compute": ("modal" if spec["executor"]["backend"] == "modal" else None, True),
            "source": ("github" if spec.get("repository") else None, False),
        }
        out: dict[str, Any] = {}
        for slot, (kind, required) in wanted.items():
            explicit = spec["connections"].get(slot)
            if kind is None:
                out[slot] = None
                continue
            if explicit:
                con = uow.get("connections", explicit, workspace_ids=[workspace_id])
                if con is None:
                    raise not_found("connection")
                if con["kind"] != kind:
                    raise DomainError(
                        "validation_failed",
                        f"{slot} connection must be of kind {kind}",
                        details={"field": f"connections.{slot}"},
                    )
                if con["config_state"] != "configured":
                    raise DomainError(
                        "connection_revoked", f"{slot} connection is {con['config_state']}"
                    )
                out[slot] = con["id"]
                continue
            candidates = [
                c
                for c in uow.find(
                    "connections",
                    {"workspace_id": workspace_id, "kind": kind, "config_state": "configured"},
                    order="priority DESC, created_at",
                )
                if c["health"] != "reauth_required"
            ]
            if not candidates and required:
                raise DomainError(
                    "connection_required",
                    f"add a {kind} Connection first",
                    details={"kind": kind, "slot": slot},
                    action="add_connection",
                )
            out[slot] = candidates[0]["id"] if candidates else None
        return out

    def _preferred_model(self, uow: Any, connection_id: str) -> str | None:
        con = uow.get("connections", connection_id)
        if not con["current_credential_version_id"]:
            return None
        latest = uow.query(
            "observations.latest",
            connection_id=connection_id,
            credential_version_id=con["current_credential_version_id"],
        )
        catalog = next((o for o in latest if o["kind"] == "catalog"), None)
        return (catalog or {}).get("safe_details", {}).get("preferred_model")
