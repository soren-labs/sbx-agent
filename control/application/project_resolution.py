"""Project-aware Session input resolution with owner-scoped Connection selection."""

from __future__ import annotations

from typing import Any

from protocol.capabilities import INFERENCE_KIND, select_endpoint

from control.application.projects import DEFAULT_SHIP_POLICY
from control.application.resolution import ExplicitResolver
from control.domain.errors import DomainError, not_found
from control.domain.identity import Principal


class ProjectResolver(ExplicitResolver):
    # Machine Slot service (set by composition); ``None`` disables subscription Sessions.
    slots: Any = None

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
        if spec["inference"]["mode"] == "subscription":
            self._bind_slot(uow, principal, workspace_id, spec)
        spec["connections"] = self._select(uow, workspace_id, spec)
        if not spec["harness"].get("model") and spec["connections"].get("inference"):
            spec["harness"]["model"] = self._config(uow, spec["connections"]["inference"]).get(
                "model"
            )
        return spec

    def _bind_slot(
        self, uow: Any, principal: Principal, workspace_id: str, spec: dict[str, Any]
    ) -> None:
        """A subscription Session runs on its Slot's Modal workspace with no API key."""
        if self.slots is None:
            raise DomainError(
                "unsupported_capability", "Machine Slots are not enabled on this deployment"
            )
        binding = self.slots.session_binding(
            uow, principal, workspace_id, spec["inference"]["machine_slot_id"], spec["harness"]
        )
        if spec["executor"]["backend"] != "modal":
            raise DomainError(
                "validation_failed",
                "a subscription Session runs on the Modal executor",
                details={"field": "executor.backend", "expected": "modal"},
            )
        compute = spec["connections"].get("compute")
        if compute and compute != binding["compute_connection_id"]:
            raise DomainError(
                "validation_failed",
                "the Session must use the Modal connection that holds the slot",
                details={"field": "connections.compute"},
            )
        spec["inference"]["machine_slot_id"] = binding["slot_id"]
        spec["connections"]["compute"] = binding["compute_connection_id"]

    def _accepted(self, provider_id: str) -> list[str]:
        return list((self.catalog.manifest(provider_id) or {}).get("inference_protocols") or [])

    def _config(self, uow: Any, connection_id: str) -> dict[str, Any]:
        con = uow.get("connections", connection_id)
        version = (
            uow.get("credential_versions", con["current_credential_version_id"])
            if con and con["current_credential_version_id"]
            else None
        )
        return (version or {}).get("public_config") or {}

    def _select(self, uow: Any, workspace_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        provider_id = spec["harness"]["provider_id"]
        accepted = self._accepted(provider_id)
        subscription = spec["inference"]["mode"] == "subscription"
        wanted: dict[str, tuple[str | None, bool]] = {
            "inference": (None if subscription else INFERENCE_KIND, True),
            "compute": ("modal" if spec["executor"]["backend"] == "modal" else None, True),
            "source": ("github" if spec.get("repository") else None, False),
        }

        def usable(con: dict[str, Any]) -> bool:
            if con["kind"] != INFERENCE_KIND:
                return True
            endpoints = self._config(uow, con["id"]).get("endpoints")
            return select_endpoint(endpoints, accepted) is not None

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
                if not usable(con):
                    raise DomainError(
                        "validation_failed",
                        f"the {provider_id} harness needs an inference connection offering "
                        f"one of: {', '.join(accepted) or 'no protocol'}",
                        details={
                            "field": f"connections.{slot}",
                            "provider_id": provider_id,
                            "inference_protocols": accepted,
                        },
                    )
                out[slot] = con["id"]
                continue
            configured = [
                c
                for c in uow.find(
                    "connections",
                    {"workspace_id": workspace_id, "kind": kind, "config_state": "configured"},
                    order="priority DESC, created_at",
                )
                if c["health"] != "reauth_required"
            ]
            candidates = [c for c in configured if usable(c)]
            if not candidates and required:
                if configured:
                    raise DomainError(
                        "connection_required",
                        f"no inference connection offers a protocol the {provider_id} harness "
                        f"accepts ({', '.join(accepted) or 'none'})",
                        details={
                            "kind": kind,
                            "slot": slot,
                            "provider_id": provider_id,
                            "inference_protocols": accepted,
                        },
                        action="add_connection",
                    )
                raise DomainError(
                    "connection_required",
                    f"add a {kind} Connection first",
                    details={"kind": kind, "slot": slot},
                    action="add_connection",
                )
            out[slot] = candidates[0]["id"] if candidates else None
        return out
