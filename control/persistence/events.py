"""Session journal + runtime ingestion offsets."""

from __future__ import annotations

from .base import Rows


class EventRepo(Rows):
    """Append-only committed journal. No update/delete paths exist."""

    def append(self, row: dict) -> dict:
        return self.insert_row("session_events", row)

    def get(self, event_id: str) -> dict | None:
        return self.one("SELECT * FROM session_events WHERE id=%s", (event_id,))

    def watermark(self, workspace_id: str, session_id: str) -> int:
        row = self.one(
            "SELECT COALESCE(MAX(seq), 0) AS wm FROM session_events"
            " WHERE workspace_id=%s AND session_id=%s",
            (workspace_id, session_id),
        )
        return row["wm"] if row else 0

    def earliest_seq(self, workspace_id: str, session_id: str) -> int | None:
        row = self.one(
            "SELECT MIN(seq) AS s FROM session_events WHERE workspace_id=%s AND session_id=%s",
            (workspace_id, session_id),
        )
        return row["s"] if row else None

    def list(
        self,
        workspace_id: str,
        session_id: str,
        *,
        after_seq: int = 0,
        types: list[str] | None = None,
        turn_id: str | None = None,
        limit: int = 500,
    ) -> tuple[list[dict], int]:
        """Committed replay. Returns (events, scanned watermark)."""
        sql = (
            "SELECT * FROM session_events WHERE workspace_id=%(w)s"
            " AND session_id=%(s)s AND seq > %(a)s"
        )
        params: dict = {"w": workspace_id, "s": session_id, "a": after_seq}
        if types:
            sql += " AND type = ANY(%(t)s)"
            params["t"] = types
        if turn_id:
            sql += " AND turn_id = %(ti)s"
            params["ti"] = turn_id
        rows = self.all(sql + " ORDER BY seq LIMIT %(lim)s", {**params, "lim": limit})
        return rows, self.watermark(workspace_id, session_id)

    def exists_runtime(self, lease_id: str, runtime_epoch: str, local_seq: int) -> dict | None:
        return self.one(
            "SELECT * FROM session_events WHERE executor_lease_id=%s"
            " AND runtime_epoch=%s AND local_seq=%s AND source='runtime'",
            (lease_id, runtime_epoch, local_seq),
        )


class IngestionOffsetRepo(Rows):
    """Durable per-source committed ack (lease, epoch) → local_seq."""

    def get_for_update(self, lease_id: str, runtime_epoch: str) -> dict | None:
        return self.one(
            "SELECT * FROM runtime_ingestion_offsets"
            " WHERE executor_lease_id=%s AND runtime_epoch=%s FOR UPDATE",
            (lease_id, runtime_epoch),
        )

    def upsert_advance(
        self,
        *,
        lease_id: str,
        runtime_epoch: str,
        session_id: str,
        committed_local_seq: int,
    ) -> dict:
        sql = (
            "INSERT INTO runtime_ingestion_offsets ("
            "id, executor_lease_id, runtime_epoch, session_id, committed_local_seq)"
            " VALUES (%(id)s, %(l)s, %(e)s, %(s)s, %(c)s)"
            " ON CONFLICT (executor_lease_id, runtime_epoch) DO UPDATE SET"
            " committed_local_seq = GREATEST("
            " runtime_ingestion_offsets.committed_local_seq, EXCLUDED.committed_local_seq),"
            " updated_at = now() RETURNING *"
        )
        return self.one_required(
            sql,
            {
                "id": _new_rio(),
                "l": lease_id,
                "e": runtime_epoch,
                "s": session_id,
                "c": committed_local_seq,
            },
        )


def _new_rio() -> str:
    from control.domain import ids

    return ids.new_id("runtime_ingestion_offset")
