"""Effective Session input resolution (RFC 02 Project and environment inputs).

Resolution order: Project defaults -> preset defaults -> caller explicit
settings, constrained by policy. The result is pinned on the Session.
"""

from __future__ import annotations

from typing import Any

from control.application.ports import HarnessCatalog
from control.domain.errors import DomainError
from control.domain.identity import Principal

EXECUTOR_BACKENDS = frozenset({"local", "modal"})
INFERENCE_MODES = frozenset({"custom_api", "subscription"})


def _require_enabled(catalog: HarnessCatalog, provider_id: str) -> dict[str, Any]:
    manifest = catalog.manifest(provider_id)
    if manifest is None:
        raise DomainError(
            "validation_failed",
            f"unknown harness {provider_id}",
            details={"field": "harness.provider_id"},
        )
    if manifest.get("support_tier") == "disabled":
        raise DomainError(
            "unsupported_capability",
            f"harness {provider_id} is disabled until install/auth/native-resume gates pass",
            details={"provider_id": provider_id, "support_tier": "disabled"},
        )
    return manifest


class ExplicitResolver:
    """Projectless/explicit resolution; Project-aware resolution extends it."""

    def __init__(self, catalog: HarnessCatalog) -> None:
        self.catalog = catalog

    def base(
        self,
        uow: Any,
        principal: Principal,
        workspace_id: str,
        body: dict[str, Any],
        defaults: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        defaults = defaults or {}
        # New Sessions default to the supported official lane; the choice is pinned explicitly.
        harness = {
            "provider_id": "opencode",
            **(defaults.get("harness") or {}),
            **(body.get("harness") or {}),
        }
        provider_id = harness.get("provider_id")
        if not provider_id:
            raise DomainError(
                "validation_failed",
                "harness.provider_id is required",
                details={"field": "harness.provider_id"},
            )
        _require_enabled(self.catalog, provider_id)
        executor = {
            "backend": "local",
            "resource_class": "standard",
            **(defaults.get("executor") or {}),
            **(body.get("executor") or {}),
        }
        if executor["backend"] not in EXECUTOR_BACKENDS:
            raise DomainError(
                "validation_failed",
                "unknown executor backend",
                details={"field": "executor.backend"},
            )
        connections = {**(defaults.get("connections") or {}), **(body.get("connections") or {})}
        repository = body.get("repository") if "repository" in body else defaults.get("repository")
        inference = body.get("inference") or {}
        mode = inference.get("mode") or (
            "subscription" if inference.get("machine_slot_id") else "custom_api"
        )
        if mode not in INFERENCE_MODES:
            raise DomainError(
                "validation_failed",
                "inference.mode must be custom_api or subscription",
                details={"field": "inference.mode"},
            )
        if mode == "subscription" and connections.get("inference"):
            # Never both: a subscription Session must not silently fall back to an API key.
            raise DomainError(
                "validation_failed",
                "a subscription Session cannot also pin an inference connection",
                details={"field": "connections.inference"},
            )
        return {
            "harness": {
                "provider_id": provider_id,
                "model": harness.get("model"),
                "effort": harness.get("effort"),
            },
            "inference": {
                "mode": mode,
                "machine_slot_id": inference.get("machine_slot_id")
                if mode == "subscription"
                else None,
            },
            "executor": executor,
            "connections": {k: connections.get(k) for k in ("compute", "inference", "source")},
            "repository": repository,
            "environment": {
                **(defaults.get("environment") or {}),
                **(body.get("environment") or {}),
            },
            "checks": list(defaults.get("checks") or body.get("checks") or []),
            "services": list(defaults.get("services") or []),
            "turn_defaults": {k: v for k, v in (defaults.get("turn_defaults") or {}).items()},
            "ship_policy": defaults.get("ship_policy"),
            "project_id": defaults.get("project_id"),
            "project_version_id": defaults.get("project_version_id"),
        }

    def resolve(
        self, uow: Any, principal: Principal, workspace_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        spec = self.base(uow, principal, workspace_id, body)
        if spec["inference"]["mode"] == "subscription":
            raise DomainError(
                "unsupported_capability", "subscription Sessions need the Project resolver"
            )
        return spec


class StaticCatalog:
    """Manifest data catalog (control consumes Harness manifests only as data)."""

    def __init__(self, manifests: list[dict[str, Any]]) -> None:
        self._by_id = {m["provider_id"]: m for m in manifests}

    def manifest(self, provider_id: str) -> dict[str, Any] | None:
        return self._by_id.get(provider_id)

    def capability(self, provider_id: str, name: str) -> str:
        manifest = self._by_id.get(provider_id) or {}
        entry = (manifest.get("capabilities") or {}).get(name)
        if isinstance(entry, dict):
            return entry.get("status", "unknown")
        return entry or "unknown"

    def providers(self) -> list[dict[str, Any]]:
        return list(self._by_id.values())
