"""Delegation, inputs, results and WaitSubscription repositories."""

from __future__ import annotations

from .base import Rows, _now


class DelegationRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("delegations", row)

    def get(self, workspace_id: str, delegation_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM delegations WHERE workspace_id=%s AND id=%s",
            (workspace_id, delegation_id),
        )

    def get_for_update(self, workspace_id: str, delegation_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM delegations WHERE workspace_id=%s AND id=%s FOR UPDATE",
            (workspace_id, delegation_id),
        )

    def get_by_child_session(self, workspace_id: str, child_session_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM delegations WHERE workspace_id=%s AND child_session_id=%s",
            (workspace_id, child_session_id),
        )

    def update(self, workspace_id: str, delegation_id: str, changes: dict) -> dict | None:
        changes = dict(changes)
        changes["updated_at"] = _now()
        return self.update_row(
            "delegations",
            {"workspace_id": workspace_id, "id": delegation_id},
            changes,
            version_column=None,
        )

    def children_of(self, workspace_id: str, parent_session_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM delegations WHERE workspace_id=%s"
            " AND parent_session_id=%s ORDER BY created_at",
            (workspace_id, parent_session_id),
        )


class DelegationInputRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("delegation_inputs", row)

    def list_for(self, workspace_id: str, delegation_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM delegation_inputs WHERE workspace_id=%s"
            " AND delegation_id=%s ORDER BY created_at",
            (workspace_id, delegation_id),
        )


class DelegationResultRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("delegation_results", row)

    def get_for(self, workspace_id: str, delegation_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM delegation_results WHERE workspace_id=%s AND delegation_id=%s",
            (workspace_id, delegation_id),
        )


class WaitSubscriptionRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("wait_subscriptions", row)

    def get(self, workspace_id: str, wait_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM wait_subscriptions WHERE workspace_id=%s AND id=%s",
            (workspace_id, wait_id),
        )

    def pending_for(self, workspace_id: str, delegation_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM wait_subscriptions WHERE workspace_id=%s"
            " AND delegation_id=%s AND state='pending'",
            (workspace_id, delegation_id),
        )

    def satisfy(
        self,
        workspace_id: str,
        wait_id: str,
        *,
        result_id: str,
        result_version: int,
    ) -> dict | None:
        return self.update_row(
            "wait_subscriptions",
            {"workspace_id": workspace_id, "id": wait_id},
            {
                "state": "satisfied",
                "satisfied_by_result_id": result_id,
                "result_version": result_version,
                "updated_at": _now(),
            },
            version_column=None,
        )

    def cancel_for_session(self, workspace_id: str, subscriber_session_id: str) -> None:
        self.conn.execute(
            "UPDATE wait_subscriptions SET state='cancelled', updated_at=now()"
            " WHERE workspace_id=%s AND subscriber_session_id=%s"
            " AND state='pending'",
            (workspace_id, subscriber_session_id),
        )
