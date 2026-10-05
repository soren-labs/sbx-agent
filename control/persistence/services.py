"""ServiceDesire / ServiceInstance repositories."""

from __future__ import annotations

from .base import Rows, _now


class ServiceDesireRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("service_desires", row)

    def get(self, workspace_id: str, desire_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM service_desires WHERE workspace_id=%s AND id=%s",
            (workspace_id, desire_id),
        )

    def list_for(self, workspace_id: str, session_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM service_desires WHERE workspace_id=%s AND session_id=%s ORDER BY name",
            (workspace_id, session_id),
        )


class ServiceInstanceRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("service_instances", row)

    def get(self, workspace_id: str, instance_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM service_instances WHERE workspace_id=%s AND id=%s",
            (workspace_id, instance_id),
        )

    def update(self, workspace_id: str, instance_id: str, changes: dict) -> dict | None:
        changes = dict(changes)
        changes["updated_at"] = _now()
        return self.update_row(
            "service_instances",
            {"workspace_id": workspace_id, "id": instance_id},
            changes,
            version_column=None,
        )

    def list_for(self, workspace_id: str, session_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM service_instances WHERE workspace_id=%s AND session_id=%s ORDER BY name",
            (workspace_id, session_id),
        )
