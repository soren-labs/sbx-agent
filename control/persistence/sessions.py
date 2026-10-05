"""Session/Message/Turn repositories (typed current-view projections)."""

from __future__ import annotations

import psycopg

from .base import Rows, _adapted, _now


class SessionRepo(Rows):
    def __init__(self, conn: psycopg.Connection) -> None:
        super().__init__(conn)

    def insert(self, row: dict) -> dict:
        return self.insert_row("sessions", row)

    def get(self, workspace_id: str, session_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM sessions WHERE workspace_id=%s AND id=%s",
            (workspace_id, session_id),
        )

    def get_for_update(self, workspace_id: str, session_id: str) -> dict | None:
        """Lock the Session row — the mutation entry point."""
        return self.one(
            "SELECT * FROM sessions WHERE workspace_id=%s AND id=%s FOR UPDATE",
            (workspace_id, session_id),
        )

    def update(
        self,
        workspace_id: str,
        session_id: str,
        changes: dict,
        *,
        expected_version: int | None = None,
    ) -> dict | None:
        changes = dict(changes)
        changes["updated_at"] = _now()
        return self.update_row(
            "sessions",
            {"workspace_id": workspace_id, "id": session_id},
            changes,
            expected_version=expected_version,
            version_column="version",
        )

    def allocate(self, workspace_id: str, session_id: str, column: str) -> int:
        """Allocate one value from a Session sequence column (row must be locked)."""
        assert column in (
            "next_event_seq",
            "next_message_ordinal",
            "next_turn_ordinal",
        )
        row = self.one(
            f"UPDATE sessions SET {column} = {column} + 1"
            " WHERE workspace_id=%s AND id=%s"
            f" RETURNING {column} - 1 AS value",
            (workspace_id, session_id),
        )
        return row["value"]

    def list(
        self,
        workspace_id: str,
        *,
        lifecycle: str | None = None,
        project_version_id: str | None = None,
        role: str | None = None,
        parent_session_id: str | None = None,
        cursor: tuple | None = None,
        limit: int = 50,
    ) -> list[dict]:
        sql = (
            "SELECT * FROM sessions WHERE workspace_id=%(w)s"
            " %(extra)s ORDER BY updated_at DESC, id LIMIT %(lim)s"
        )
        clauses = []
        params = {"w": workspace_id, "lim": limit}
        if lifecycle:
            clauses.append("AND lifecycle = %(lifecycle)s")
            params["lifecycle"] = lifecycle
        if project_version_id:
            clauses.append("AND project_version_id = %(pv)s")
            params["pv"] = project_version_id
        if role:
            clauses.append("AND role = %(role)s")
            params["role"] = role
        if parent_session_id:
            clauses.append(
                "AND id IN (SELECT child_session_id FROM delegations"
                " WHERE parent_session_id = %(ps)s)"
            )
            params["ps"] = parent_session_id
        if cursor:
            clauses.append("AND (updated_at, id) < (%(ca)s, %(ci)s)")
            params["ca"], params["ci"] = cursor
        return self.all(sql.replace("%(extra)s", " ".join(clauses)), params)


class MessageRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("messages", row)

    def get(self, workspace_id: str, message_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM messages WHERE workspace_id=%s AND id=%s",
            (workspace_id, message_id),
        )

    def list_by_session(
        self,
        workspace_id: str,
        session_id: str,
        *,
        limit: int = 200,
        after_ordinal: int | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM messages WHERE workspace_id=%(w)s AND session_id=%(s)s"
        params = {"w": workspace_id, "s": session_id, "lim": limit}
        if after_ordinal is not None:
            sql += " AND ordinal > %(o)s"
            params["o"] = after_ordinal
        return self.all(sql + " ORDER BY ordinal LIMIT %(lim)s", params)


class TurnRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("turns", row)

    def get(self, workspace_id: str, turn_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM turns WHERE workspace_id=%s AND id=%s",
            (workspace_id, turn_id),
        )

    def get_for_update(self, workspace_id: str, turn_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM turns WHERE workspace_id=%s AND id=%s FOR UPDATE",
            (workspace_id, turn_id),
        )

    def get_by_message(self, workspace_id: str, message_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM turns WHERE workspace_id=%s AND message_id=%s",
            (workspace_id, message_id),
        )

    def update(
        self, workspace_id: str, turn_id: str, changes: dict, *, expected_version: int | None = None
    ) -> dict | None:
        changes = dict(changes)
        changes["updated_at"] = _now()
        return self.update_row(
            "turns",
            {"workspace_id": workspace_id, "id": turn_id},
            changes,
            expected_version=expected_version,
            version_column="version",
        )

    def active_by_session(self, workspace_id: str, session_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM turns WHERE workspace_id=%s AND session_id=%s"
            " AND state IN ('preparing','running','cancelling')",
            (workspace_id, session_id),
        )

    def queued_by_session(self, workspace_id: str, session_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM turns WHERE workspace_id=%s AND session_id=%s"
            " AND state='queued' ORDER BY ordinal",
            (workspace_id, session_id),
        )

    def list_by_session(self, workspace_id: str, session_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM turns WHERE workspace_id=%s AND session_id=%s ORDER BY ordinal",
            (workspace_id, session_id),
        )


class MessagePartRepo(Rows):
    def upsert(
        self,
        row: dict,
        *,
        seal: bool = False,
    ) -> dict:
        """Revisioned part upsert: same part_id bumps revision; a replacement
        content sets new revision, a delta appends server-side."""
        sql = (
            "INSERT INTO message_parts ("
            "id, workspace_id, session_id, message_id, part_id, ordinal, kind,"
            " revision, content, sealed, completeness) VALUES ("
            "%(id)s, %(workspace_id)s, %(session_id)s, %(message_id)s, %(part_id)s,"
            " %(ordinal)s, %(kind)s, %(revision)s, %(content)s, %(sealed)s,"
            " %(completeness)s)"
            " ON CONFLICT (message_id, part_id) DO UPDATE SET"
            " revision = message_parts.revision + 1,"
            " content = EXCLUDED.content,"
            " sealed = EXCLUDED.sealed OR message_parts.sealed,"
            " completeness = EXCLUDED.completeness,"
            " updated_at = now()"
            " RETURNING *"
        )
        row = dict(row)
        row["sealed"] = seal or row.get("sealed", False)
        return self.one_required(sql, _adapted(row))

    def list_by_message(self, workspace_id: str, message_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM message_parts WHERE workspace_id=%s AND message_id=%s ORDER BY ordinal",
            (workspace_id, message_id),
        )

    def list_by_session(self, workspace_id: str, session_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM message_parts WHERE workspace_id=%s AND session_id=%s"
            " ORDER BY message_id, ordinal",
            (workspace_id, session_id),
        )
