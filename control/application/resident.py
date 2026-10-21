"""Resident machines: discovery for workspace members, registration for the operator.

Members of the owning workspace can list and inspect a machine and pin it on a Session;
they never see or set its SSH target. Registration and removal are operator commands
(``sbx server machine ...``) that run with the server's own configuration, so no API
caller can choose a host, alias, key, known_hosts file, path or remote command.

Discovery is read-only: the live probe is a bounded SSH check whose result is only
cached in memory; nothing is written to PostgreSQL.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from control.application import access
from control.domain.errors import DomainError, not_found
from control.domain.execution import LIVE_LEASE_STATES
from control.domain.identity import Principal
from control.domain.ids import new_id

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
UNCHECKED = {"status": "unchecked", "reason": None, "runtime": None, "checked_at": None}


def occupying_leases(uow: Any, machine_id: str) -> list[dict[str, Any]]:
    """Leases whose runtime may still run on the machine: live ones, plus lost ones whose
    termination is not confirmed yet (quarantined). Capacity, the API count and removal all
    use this one rule, so an unconfirmed runtime is never treated as a free slot."""
    rows = uow.find(
        "executor_leases", {"resident_machine_id": machine_id, "state": list(LIVE_LEASE_STATES)}
    ) + uow.find("executor_leases", {"resident_machine_id": machine_id, "quarantined": True})
    return list({row["id"]: row for row in rows}.values())


class ResidentMachines:
    def __init__(
        self, tx: Any, prober: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    ) -> None:
        self.tx = tx
        # Set when the resident executor is enabled; probes one target (SSH, read-only).
        self.prober = prober

    # ---------------------------------------------------------------- member views
    def _view(
        self, uow: Any, machine: dict[str, Any], reachability: dict[str, Any] | None
    ) -> dict[str, Any]:
        occupying = occupying_leases(uow, machine["id"])
        return {
            "id": machine["id"],
            "workspace_id": machine["workspace_id"],
            "name": machine["name"],
            "state": machine["state"],
            "executor_backend": "resident",
            # live_leases counts every runtime not confirmed stopped (quarantined included).
            "capacity": {
                "max_leases": machine["max_leases"],
                "live_leases": len(occupying),
                "unconfirmed_leases": sum(1 for row in occupying if row["quarantined"]),
            },
            "reachability": reachability or UNCHECKED,
            "created_at": machine["created_at"].isoformat(),
        }

    def _probe(self, machine: dict[str, Any]) -> dict[str, Any]:
        if self.prober is None:
            return {
                **UNCHECKED,
                "reason": "the resident executor is not enabled on this server",
            }
        if machine["state"] != "active":
            return {**UNCHECKED, "reason": f"machine is {machine['state']}"}
        return self.prober(machine["target"])

    def list(
        self, principal: Principal, workspace_id: str, *, probe: bool = True
    ) -> dict[str, Any]:
        access.require_workspace(principal, workspace_id)
        rows = self.tx.read(
            lambda uow: uow.find(
                "resident_machines",
                {"workspace_id": workspace_id, "state": ["active", "disabled"]},
                order="name",
            )
        )
        results: dict[str, dict[str, Any] | None] = {r["id"]: None for r in rows}
        if probe and rows:
            with ThreadPoolExecutor(max_workers=min(8, len(rows))) as pool:
                for row, result in zip(rows, pool.map(self._probe, rows), strict=True):
                    results[row["id"]] = result
        return self.tx.read(
            lambda uow: {"items": [self._view(uow, r, results[r["id"]]) for r in rows]}
        )

    def get(self, principal: Principal, machine_id: str, *, probe: bool = True) -> dict[str, Any]:
        row = self.tx.read(
            lambda uow: access.owned(
                uow, principal, "resident_machines", machine_id, what="resident machine"
            )
        )
        if row["state"] == "removed":
            raise not_found("resident machine")
        result = self._probe(row) if probe else None
        return self.tx.read(lambda uow: self._view(uow, row, result))

    # ------------------------------------------------------------- operator commands
    def register(
        self,
        workspace_id: str,
        name: str,
        target: dict[str, Any],
        *,
        max_leases: int = 4,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        if not NAME.match(name or ""):
            raise DomainError(
                "validation_failed",
                "name must be 1-63 lowercase letters, digits, '.', '_' or '-'",
                details={"field": "name"},
            )
        if not 1 <= int(max_leases) <= 64:
            raise DomainError("validation_failed", "max_leases must be 1..64")

        def fn(uow: Any) -> dict[str, Any]:
            if uow.get("workspaces", workspace_id) is None:
                raise not_found("workspace")
            if uow.find_one(
                "resident_machines",
                {"workspace_id": workspace_id, "name": name, "state": ["active", "disabled"]},
            ):
                raise DomainError(
                    "validation_failed",
                    f"a resident machine named {name} already exists in this workspace",
                    details={"field": "name"},
                )
            row = uow.insert(
                "resident_machines",
                {
                    "id": new_id("resident_machine"),
                    "workspace_id": workspace_id,
                    "name": name,
                    "target": target,
                    "max_leases": int(max_leases),
                    "created_by": created_by,
                },
            )
            uow.audit(
                workspace_id=workspace_id,
                actor="operator",
                action="resident_machine.register",
                target_kind="resident_machine",
                target_id=row["id"],
                result="succeeded",
            )
            return row

        return self.tx.run(fn)

    def operator_list(self) -> list[dict[str, Any]]:
        return self.tx.read(
            lambda uow: uow.find(
                "resident_machines", {"state": ["active", "disabled"]}, order="workspace_id, name"
            )
        )

    def find(self, ref: str, workspace_id: str | None = None) -> dict[str, Any]:
        """A live machine by id or by name (name needs to be unique across workspaces)."""

        def fn(uow: Any) -> dict[str, Any]:
            where: dict[str, Any] = {"state": ["active", "disabled"]}
            if workspace_id:
                where["workspace_id"] = workspace_id
            rows = uow.find("resident_machines", {**where, "id": ref}) or uow.find(
                "resident_machines", {**where, "name": ref}
            )
            if not rows:
                raise not_found("resident machine")
            if len(rows) > 1:
                raise DomainError(
                    "validation_failed", f"{ref} names machines in several workspaces; use the id"
                )
            return rows[0]

        return self.tx.read(fn)

    def remove(self, machine_id: str) -> dict[str, Any]:
        """Removal never strands a runtime: refused while any lease is not released."""

        def fn(uow: Any) -> dict[str, Any]:
            row = uow.get("resident_machines", machine_id, lock=True)
            if row is None or row["state"] == "removed":
                raise not_found("resident machine")
            live = occupying_leases(uow, machine_id)
            if live:
                raise DomainError(
                    "invalid_transition",
                    "release the Sessions running on this machine first",
                    details={"leases": sorted({lease["id"] for lease in live})},
                )
            row = uow.update(
                "resident_machines",
                machine_id,
                {"state": "removed", "removed_at": uow.now(), "updated_at": uow.now()},
            )
            uow.audit(
                workspace_id=row["workspace_id"],
                actor="operator",
                action="resident_machine.remove",
                target_kind="resident_machine",
                target_id=machine_id,
                result="succeeded",
            )
            return row

        return self.tx.run(fn)
