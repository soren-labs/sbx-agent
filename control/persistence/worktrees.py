"""Worktree, barrier operations and Snapshot repositories."""

from __future__ import annotations

from .base import Rows, _now


class WorktreeRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("worktrees", row)

    def get(self, workspace_id: str, worktree_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM worktrees WHERE workspace_id=%s AND id=%s",
            (workspace_id, worktree_id),
        )

    def get_by_session(self, workspace_id: str, session_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM worktrees WHERE workspace_id=%s AND session_id=%s",
            (workspace_id, session_id),
        )

    def get_for_update(self, workspace_id: str, worktree_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM worktrees WHERE workspace_id=%s AND id=%s FOR UPDATE",
            (workspace_id, worktree_id),
        )

    def update(self, workspace_id: str, worktree_id: str, changes: dict) -> dict | None:
        changes = dict(changes)
        changes["updated_at"] = _now()
        return self.update_row(
            "worktrees",
            {"workspace_id": workspace_id, "id": worktree_id},
            changes,
            version_column=None,
        )


class WorktreeOpRepo(Rows):
    """Exclusive mutation barriers — partial unique active per Worktree."""

    def insert(self, row: dict) -> dict:
        return self.insert_row("worktree_operations", row)

    def get(self, workspace_id: str, op_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM worktree_operations WHERE workspace_id=%s AND id=%s",
            (workspace_id, op_id),
        )

    def get_by_operation(self, operation_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM worktree_operations WHERE operation_id=%s",
            (operation_id,),
        )

    def active_by_worktree(self, workspace_id: str, worktree_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM worktree_operations WHERE workspace_id=%s"
            " AND worktree_id=%s AND state='active'",
            (workspace_id, worktree_id),
        )

    def complete(self, workspace_id: str, op_id: str, state: str) -> dict | None:
        return self.update_row(
            "worktree_operations",
            {"workspace_id": workspace_id, "id": op_id},
            {"state": state, "updated_at": _now()},
            version_column=None,
        )


class SnapshotRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("snapshots", row)

    def get(self, workspace_id: str, snapshot_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM snapshots WHERE workspace_id=%s AND id=%s",
            (workspace_id, snapshot_id),
        )

    def update(self, workspace_id: str, snapshot_id: str, changes: dict) -> dict | None:
        changes = dict(changes)
        changes["updated_at"] = _now()
        return self.update_row(
            "snapshots",
            {"workspace_id": workspace_id, "id": snapshot_id},
            changes,
            version_column=None,
        )

    def list_by_session(self, workspace_id: str, worktree_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM snapshots WHERE workspace_id=%s AND worktree_id=%s ORDER BY created_at",
            (workspace_id, worktree_id),
        )

    def environment_by_key(
        self, workspace_id: str, project_version_id: str, input_digest: str
    ) -> dict | None:
        return self.one(
            "SELECT * FROM snapshots WHERE workspace_id=%s AND kind='environment'"
            " AND project_version_id=%s AND input_digest=%s AND state='ready'"
            " ORDER BY created_at DESC LIMIT 1",
            (workspace_id, project_version_id, input_digest),
        )
