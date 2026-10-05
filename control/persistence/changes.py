"""ChangeSet + ChangeSetFile repositories (immutable captures)."""

from __future__ import annotations

from .base import Rows


class ChangeSetRepo(Rows):
    """ChangeSets are immutable: insert + read-only. Flag columns may be set
    once (delivery_state is derived elsewhere); mutation beyond flags is
    rejected by callers, not by the DB."""

    def insert(self, row: dict) -> dict:
        return self.insert_row("changesets", row)

    def get(self, workspace_id: str, changeset_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM changesets WHERE workspace_id=%s AND id=%s",
            (workspace_id, changeset_id),
        )

    def get_by_digest(self, workspace_id: str, subject_digest: str) -> dict | None:
        return self.one(
            "SELECT * FROM changesets WHERE workspace_id=%s AND subject_digest=%s",
            (workspace_id, subject_digest),
        )

    def get_by_capture(
        self,
        workspace_id: str,
        session_id: str,
        source_turn_id: str,
        worktree_generation: int,
    ) -> dict | None:
        """The durable capture dedupe key: (session, source turn, wt generation)."""
        return self.one(
            "SELECT * FROM changesets WHERE workspace_id=%s AND session_id=%s"
            " AND source_turn_id=%s AND worktree_generation=%s",
            (workspace_id, session_id, source_turn_id, worktree_generation),
        )

    def list_by_session(self, workspace_id: str, session_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM changesets WHERE workspace_id=%s AND session_id=%s ORDER BY created_at",
            (workspace_id, session_id),
        )


class ChangeSetFileRepo(Rows):
    """File rows hang off the ChangeSet (no workspace scoping of their own)."""

    def insert(self, row: dict) -> dict:
        return self.insert_row("changeset_files", row)

    def bulk(self, rows: list[dict]) -> None:
        for row in rows:
            self.insert_row("changeset_files", row)

    def list_for(self, changeset_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM changeset_files WHERE changeset_id=%s ORDER BY path",
            (changeset_id,),
        )
