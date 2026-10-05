"""Connection / CredentialVersion / grants / observations."""

from __future__ import annotations

from .base import Rows, _now


class ConnectionRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("connections", row)

    def get(self, workspace_id: str, connection_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM connections WHERE workspace_id=%s AND id=%s",
            (workspace_id, connection_id),
        )

    def get_for_update(self, workspace_id: str, connection_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM connections WHERE workspace_id=%s AND id=%s FOR UPDATE",
            (workspace_id, connection_id),
        )

    def find_by_kind(
        self, workspace_id: str, kind: str, owner_user_id: str | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM connections WHERE workspace_id=%s AND kind=%s"
        params: list = [workspace_id, kind]
        if owner_user_id:
            sql += " AND owner_user_id=%s"
            params.append(owner_user_id)
        return self.all(sql, params)

    def update(self, workspace_id: str, connection_id: str, changes: dict) -> dict | None:
        changes = dict(changes)
        changes["updated_at"] = _now()
        return self.update_row(
            "connections",
            {"workspace_id": workspace_id, "id": connection_id},
            changes,
            version_column=None,
        )

    def list(self, workspace_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM connections WHERE workspace_id=%s ORDER BY created_at",
            (workspace_id,),
        )


class CredentialVersionRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("credential_versions", row)

    def get(self, workspace_id: str, credential_version_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM credential_versions WHERE workspace_id=%s AND id=%s",
            (workspace_id, credential_version_id),
        )

    def current_for(self, workspace_id: str, connection_id: str) -> dict | None:
        return self.one(
            "SELECT cv.* FROM credential_versions cv JOIN connections c"
            " ON c.current_credential_version_id = cv.id"
            " WHERE cv.workspace_id=%s AND c.id=%s",
            (workspace_id, connection_id),
        )


class CredentialGrantRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("credential_grants", row)

    def active_for(self, workspace_id: str, credential_version_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM credential_grants WHERE workspace_id=%s"
            " AND credential_version_id=%s AND revoked_at IS NULL",
            (workspace_id, credential_version_id),
        )

    def revoke_for_connection(self, workspace_id: str, connection_id: str) -> None:
        self.conn.execute(
            "UPDATE credential_grants SET revoked_at=now() WHERE workspace_id=%s"
            " AND connection_id=%s AND revoked_at IS NULL",
            (workspace_id, connection_id),
        )


class ConnectionObservationRepo(Rows):
    """Append-only validation/health evidence per connection."""

    def insert(self, row: dict) -> dict:
        return self.insert_row("connection_observations", row)

    def list_for(self, workspace_id: str, connection_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM connection_observations WHERE workspace_id=%s"
            " AND connection_id=%s ORDER BY created_at",
            (workspace_id, connection_id),
        )
