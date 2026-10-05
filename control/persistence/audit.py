"""Append-only audit records — sensitive-path evidence."""

from __future__ import annotations

from .base import Rows


class AuditRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("audit_records", row)

    def list_for(self, workspace_id: str, target_kind: str, target_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM audit_records WHERE workspace_id=%s"
            " AND target_kind=%s AND target_id=%s ORDER BY created_at",
            (workspace_id, target_kind, target_id),
        )
