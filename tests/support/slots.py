"""Scripted stand-in for the Modal executor's Setup VM, Worker and Volume operations.

Shared by the Machine Slot API and SDK/CLI tests; the real cloud path is covered by the
opt-in ``tests/e2e_modal`` checks.
"""

from __future__ import annotations

import itertools
from typing import Any

from control.domain.errors import DomainError

MODAL = {"token_id": "ak-test0000000000001", "token_secret": "as-secret00000000000001"}
URL = "https://auth.openai.com/codex/device"
CODE = "ABCD-12345"


CATALOG = {
    "complete": True,
    "items": [
        {
            "id": "model-a",
            "displayName": "Model A",
            "isDefault": True,
            "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [
                {"reasoningEffort": "low", "description": "Fast"},
                {"reasoningEffort": "medium", "description": "Balanced"},
                {"reasoningEffort": "ultra", "description": "Maximum"},
            ],
        },
        {
            "id": "model-b",
            "displayName": "Model B",
            "defaultReasoningEffort": "low",
            "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": "Fast"}],
        },
    ],
}


class SetupCloud:
    """In-memory stand-in for the Modal executor's Setup VM and Volume operations."""

    catalog: Any = CATALOG

    def __init__(self) -> None:
        self.ids = itertools.count(1)
        self.vms: dict[str, dict[str, Any]] = {}
        self.volumes: set[str] = set()
        self.deleted_volumes: list[str] = []
        self.terminated: list[str] = []
        self.starts = 0
        self.allocations: list[dict[str, Any]] = []
        self.synced: list[tuple[str, str]] = []
        self.max_mounts = 0

    def setup_start(self, spec: dict[str, Any], operation_id: str) -> dict[str, Any]:
        assert set(spec["compute"]) >= {"token_id", "token_secret"}
        existing = self.lookup(operation_id, spec["compute"])
        if existing:
            return existing
        self.starts += 1
        vm = {
            "sandbox_id": f"sb-{next(self.ids)}",
            "operation_id": operation_id,
            "spec": spec,
            "alive": True,
            "state": {"phase": "starting", "mode": spec["setup"]["mode"]},
        }
        self.vms[vm["sandbox_id"]] = vm
        self.volumes.add(spec["volume_name"])
        return {"sandbox_id": vm["sandbox_id"], "operation_id": operation_id, "status": "running"}

    def lookup(self, operation_id: str, compute: Any) -> dict[str, Any] | None:
        for vm in self.vms.values():
            if vm["operation_id"] == operation_id:
                return {
                    "sandbox_id": vm["sandbox_id"],
                    "operation_id": operation_id,
                    "status": "running" if vm["alive"] else "terminated",
                }
        return None

    def setup_observe(self, handle: dict[str, Any], compute: Any, path: str) -> dict[str, Any]:
        vm = self.vms[handle["sandbox_id"]]
        if not vm["alive"]:
            return {"status": "terminated", "state": None}
        return {"status": "running", "state": dict(vm["state"])}

    def terminate(self, handle: dict[str, Any], operation_id: str, compute: Any) -> bool:
        self.vms[handle["sandbox_id"]]["alive"] = False
        self.terminated.append(handle["sandbox_id"])
        return True

    # -- Worker allocation (the runtime never becomes reachable in these tests) -----------
    def allocate(self, spec: dict[str, Any], operation_id: str) -> dict[str, Any]:
        self.allocations.append(spec)
        vm = {
            "sandbox_id": f"sb-{next(self.ids)}",
            "operation_id": operation_id,
            "spec": {"tags": {"sbx_slot": None}},
            "alive": True,
            "state": {},
        }
        self.vms[vm["sandbox_id"]] = vm
        vm["volume"] = (spec.get("profile") or {}).get("volume_name")
        mounted = [v for v in self.vms.values() if v["alive"] and v.get("volume") == vm["volume"]]
        if vm["volume"]:
            self.max_mounts = max(self.max_mounts, len(mounted))
        return {
            "sandbox_id": vm["sandbox_id"],
            "operation_id": operation_id,
            "lease_id": spec["lease_id"],
            "status": "running",
        }

    def describe(self, handle: dict[str, Any], compute: Any) -> dict[str, Any]:
        alive = self.vms[handle["sandbox_id"]]["alive"]
        return {"status": "running" if alive else "terminated"}

    def connect_runtime(self, handle: dict[str, Any], compute: Any) -> str:
        raise DomainError("executor_unavailable", "runtime not reachable yet", retryable=True)

    def profile_sync(self, handle: dict[str, Any], compute: Any, mount: str) -> bool:
        self.synced.append((handle["sandbox_id"], mount))
        return True

    def volume_delete(self, compute: Any, name: str) -> bool:
        self.volumes.discard(name)
        self.deleted_volumes.append(name)
        return True

    # -- scripting ---------------------------------------------------------------------
    def vm_for(self, slot: dict[str, Any]) -> dict[str, Any]:
        return next(
            vm
            for vm in reversed(list(self.vms.values()))
            if vm["spec"]["tags"]["sbx_slot"] == slot["id"]
        )

    def show_code(self, slot: dict[str, Any]) -> None:
        self.vm_for(slot)["state"].update(
            phase="awaiting_user",
            verification_url=URL,
            user_code=CODE,
            code_expires_at="2099-01-01T00:00:00+00:00",
        )

    def finish(self, slot: dict[str, Any], phase: str, error: str | None = None) -> None:
        state = self.vm_for(slot)["state"]
        state.pop("user_code", None)
        state.update(phase=phase, error=error)
        if phase == "succeeded":
            state.update(
                cli_version="codex-cli 0.162.0",
                real_model_call=True,
                profile_synced=True,
                catalog=self.catalog,
            )

    @property
    def running(self) -> list[str]:
        return [vm["sandbox_id"] for vm in self.vms.values() if vm["alive"]]
