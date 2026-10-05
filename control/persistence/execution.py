"""Execution, ExecutorLease, native bindings, fences and capacity."""

from __future__ import annotations

from .base import Rows, _now


class LeaseRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("executor_leases", row)

    def get(self, workspace_id: str, lease_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM executor_leases WHERE workspace_id=%s AND id=%s",
            (workspace_id, lease_id),
        )

    def get_for_update(self, workspace_id: str, lease_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM executor_leases WHERE workspace_id=%s AND id=%s FOR UPDATE",
            (workspace_id, lease_id),
        )

    def get_by_allocation(self, allocation_operation_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM executor_leases WHERE allocation_operation_id=%s",
            (allocation_operation_id,),
        )

    def active_by_session(self, workspace_id: str, session_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM executor_leases WHERE workspace_id=%s AND session_id=%s"
            " AND state IN ('allocating','ready','quiescing')",
            (workspace_id, session_id),
        )

    def update(self, workspace_id: str, lease_id: str, changes: dict) -> dict | None:
        changes = dict(changes)
        changes["updated_at"] = _now()
        return self.update_row(
            "executor_leases",
            {"workspace_id": workspace_id, "id": lease_id},
            changes,
            version_column=None,
        )

    def list_expired(self, before) -> list[dict]:
        return self.all(
            "SELECT * FROM executor_leases WHERE state IN"
            " ('allocating','ready','quiescing') AND expires_at < %s",
            (before,),
        )


class ExecutionRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("executions", row)

    def get(self, workspace_id: str, execution_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM executions WHERE workspace_id=%s AND id=%s",
            (workspace_id, execution_id),
        )

    def get_for_update(self, workspace_id: str, execution_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM executions WHERE workspace_id=%s AND id=%s FOR UPDATE",
            (workspace_id, execution_id),
        )

    def get_by_operation(self, operation_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM executions WHERE operation_id=%s",
            (operation_id,),
        )

    def nonterminal_by_turn(self, workspace_id: str, turn_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM executions WHERE workspace_id=%s AND turn_id=%s"
            " AND state IN ('preparing','started','stop_requested')",
            (workspace_id, turn_id),
        )

    def next_ordinal(self, workspace_id: str, turn_id: str) -> int:
        row = self.one(
            "SELECT COALESCE(MAX(attempt_ordinal), 0) + 1 AS n"
            " FROM executions WHERE workspace_id=%s AND turn_id=%s",
            (workspace_id, turn_id),
        )
        return row["n"]

    def update(self, workspace_id: str, execution_id: str, changes: dict) -> dict | None:
        changes = dict(changes)
        changes["updated_at"] = _now()
        return self.update_row(
            "executions",
            {"workspace_id": workspace_id, "id": execution_id},
            changes,
            version_column=None,
        )

    def list_by_turn(self, workspace_id: str, turn_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM executions WHERE workspace_id=%s AND turn_id=%s"
            " ORDER BY attempt_ordinal",
            (workspace_id, turn_id),
        )


class NativeBindingRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("native_context_bindings", row)

    def get(self, workspace_id: str, binding_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM native_context_bindings WHERE workspace_id=%s AND id=%s",
            (workspace_id, binding_id),
        )

    def find(
        self, workspace_id: str, session_id: str, provider_id: str, lineage_id: str
    ) -> dict | None:
        return self.one(
            "SELECT * FROM native_context_bindings WHERE workspace_id=%s"
            " AND session_id=%s AND provider_id=%s AND lineage_id=%s"
            " ORDER BY created_at DESC LIMIT 1",
            (workspace_id, session_id, provider_id, lineage_id),
        )


class FenceRepo(Rows):
    """Named resource fences — lease/resource generation guards."""

    def acquire(
        self,
        *,
        fence_id: str,
        workspace_id: str,
        resource_name: str,
        fence_type: str,
        holder: str,
        expires_at,
    ) -> dict | None:
        """Insert-or-bump fence; returns row when held by caller."""
        row = self.one(
            "INSERT INTO resource_fences (id, workspace_id, resource_name,"
            " fence_type, generation, holder, expires_at)"
            " VALUES (%(id)s, %(w)s, %(r)s, %(t)s, 1, %(h)s, %(e)s)"
            " ON CONFLICT (workspace_id, resource_name) DO NOTHING RETURNING *",
            {
                "id": fence_id,
                "w": workspace_id,
                "r": resource_name,
                "t": fence_type,
                "h": holder,
                "e": expires_at,
            },
        )
        if row is not None:
            return row
        # Existing fence: bump generation iff holder matches or expired.
        return self.one(
            "UPDATE resource_fences SET generation = generation + 1,"
            " holder=%(h)s, expires_at=%(e)s, updated_at=now()"
            " WHERE workspace_id=%(w)s AND resource_name=%(r)s"
            " AND (holder=%(h)s OR expires_at < now()) RETURNING *",
            {"w": workspace_id, "r": resource_name, "h": holder, "e": expires_at},
        )

    def get_for_update(self, workspace_id: str, resource_name: str) -> dict | None:
        return self.one(
            "SELECT * FROM resource_fences WHERE workspace_id=%s AND resource_name=%s FOR UPDATE",
            (workspace_id, resource_name),
        )

    def check(self, workspace_id: str, resource_name: str, generation: int) -> bool:
        row = self.one(
            "SELECT generation FROM resource_fences WHERE workspace_id=%s AND resource_name=%s",
            (workspace_id, resource_name),
        )
        return row is not None and row["generation"] == generation


class CapacityRepo(Rows):
    def hold(
        self,
        *,
        reservation_id: str,
        workspace_id: str,
        connection_id: str,
        slot_ordinal: int,
        execution_id: str | None,
        lease_id: str | None,
        expires_at,
    ) -> dict | None:
        """Reserve a scoped compute slot; unique active (connection, slot)."""
        return self.one(
            "INSERT INTO capacity_reservations (id, workspace_id, connection_id,"
            " slot_ordinal, execution_id, executor_lease_id, state, expires_at)"
            " VALUES (%(id)s, %(w)s, %(c)s, %(s)s, %(e)s, %(l)s, 'held', %(x)s)"
            " ON CONFLICT (connection_id, slot_ordinal) WHERE state='held'"
            " DO NOTHING RETURNING *",
            {
                "id": reservation_id,
                "w": workspace_id,
                "c": connection_id,
                "s": slot_ordinal,
                "e": execution_id,
                "l": lease_id,
                "x": expires_at,
            },
        )

    def held_slots(self, workspace_id: str, connection_id: str) -> int:
        row = self.one(
            "SELECT count(*) AS n FROM capacity_reservations"
            " WHERE workspace_id=%s AND connection_id=%s AND state='held'",
            (workspace_id, connection_id),
        )
        return row["n"]

    def release(self, workspace_id: str, reservation_id: str) -> dict | None:
        return self.update_row(
            "capacity_reservations",
            {"workspace_id": workspace_id, "id": reservation_id},
            {"state": "released"},
            version_column=None,
        )
