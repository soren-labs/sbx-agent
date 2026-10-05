"""Transactional outbox — notifications, webhooks and waiter wakeups."""

from __future__ import annotations

from .base import Rows


class OutboxRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("outbox_messages", row)

    def get(self, workspace_id: str, outbox_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM outbox_messages WHERE workspace_id=%s AND id=%s",
            (workspace_id, outbox_id),
        )

    def get_by_dedupe(self, dedupe_key: str) -> dict | None:
        return self.one("SELECT * FROM outbox_messages WHERE dedupe_key=%s", (dedupe_key,))

    def due(self, limit: int = 50) -> list[dict]:
        return self.all(
            "SELECT * FROM outbox_messages WHERE state='queued' AND due_at <= now()"
            " ORDER BY created_at LIMIT %s",
            (limit,),
        )

    def mark(
        self,
        workspace_id: str,
        outbox_id: str,
        state: str,
        *,
        error: dict | None = None,
    ) -> dict | None:
        changes: dict = {"state": state, "attempts": _attempt_inc()}
        if error:
            changes["payload"] = _payload_error(error)
        return self.update_row(
            "outbox_messages",
            {"workspace_id": workspace_id, "id": outbox_id},
            changes,
            version_column=None,
        )


def _attempt_inc():
    from .base import sqlexpr

    return sqlexpr("attempts + 1")


def _payload_error(error: dict):
    import json

    from .base import sqlexpr

    return sqlexpr("payload || " + _quote(json.dumps({"last_error": error})))


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'::jsonb"
