"""Project, ProjectVersion and EnvironmentBuild repositories."""

from __future__ import annotations

from .base import Rows, _now


class ProjectRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("projects", row)

    def get(self, workspace_id: str, project_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM projects WHERE workspace_id=%s AND id=%s",
            (workspace_id, project_id),
        )

    def get_by_slug(self, workspace_id: str, slug: str) -> dict | None:
        return self.one(
            "SELECT * FROM projects WHERE workspace_id=%s AND slug=%s",
            (workspace_id, slug),
        )

    def update(self, workspace_id: str, project_id: str, changes: dict) -> dict | None:
        changes = dict(changes)
        changes["updated_at"] = _now()
        return self.update_row(
            "projects",
            {"workspace_id": workspace_id, "id": project_id},
            changes,
            version_column=None,
        )

    def list(self, workspace_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM projects WHERE workspace_id=%s ORDER BY created_at",
            (workspace_id,),
        )


class ProjectVersionRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("project_versions", row)

    def get(self, workspace_id: str, project_version_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM project_versions WHERE workspace_id=%s AND id=%s",
            (workspace_id, project_version_id),
        )

    def get_by_digest(self, workspace_id: str, project_id: str, digest: str) -> dict | None:
        return self.one(
            "SELECT * FROM project_versions WHERE workspace_id=%s AND project_id=%s AND digest=%s",
            (workspace_id, project_id, digest),
        )

    def list_by_project(self, workspace_id: str, project_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM project_versions WHERE workspace_id=%s"
            " AND project_id=%s ORDER BY created_at",
            (workspace_id, project_id),
        )


class EnvironmentBuildRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("environment_builds", row)

    def get(self, workspace_id: str, build_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM environment_builds WHERE workspace_id=%s AND id=%s",
            (workspace_id, build_id),
        )

    def get_by_operation(self, operation_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM environment_builds WHERE operation_id=%s",
            (operation_id,),
        )

    def update(self, workspace_id: str, build_id: str, changes: dict) -> dict | None:
        changes = dict(changes)
        changes["updated_at"] = _now()
        return self.update_row(
            "environment_builds",
            {"workspace_id": workspace_id, "id": build_id},
            changes,
            version_column=None,
        )
