from control.application.access import owned, workspace
from control.application.deduplication import command
from control.domain.errors import require
from control.domain.events import digest
from control.domain.identity import new_id
from control.domain.projects import repository_identity, validate_environment


class Projects:
    def __init__(self, uow):
        self.uow = uow

    def create(self, principal, wid, body, key):
        workspace(principal, wid)
        repository = repository_identity(body["repository"])
        spec = validate_environment(body.get("spec", {}))
        with self.uow.transaction() as repo:

            def perform():
                pid, pver = new_id("prj"), new_id("pver")
                repo.execute(
                    "INSERT INTO projects(id,workspace_id,slug,name) VALUES(%s,%s,%s,%s)",
                    (pid, wid, body.get("slug", pid), body.get("name", "Project")),
                )
                repo.execute(
                    "INSERT INTO project_versions(id,workspace_id,project_id,ordinal,repository,"
                    "base_ref,spec,spec_digest) VALUES(%s,%s,%s,1,%s,%s,%s,%s)",
                    (pver, wid, pid, repository, body.get("base_ref", "main"), spec, digest(spec)),
                )
                repo.execute("UPDATE projects SET current_version_id=%s WHERE id=%s", (pver, pid))
                return {"project_id": pid, "project_version_id": pver, "version": 1}

            return command(repo, principal, wid, "project.create", key, body, perform)

    def publish(self, principal, pid, body, key):
        with self.uow.transaction() as repo:
            project = owned(repo, "projects", pid, principal, lock=True)

            def perform():
                require(body["expected_version"] == project["version"], "version_conflict")
                pver = new_id("pver")
                spec = validate_environment(body["spec"])
                repository = repository_identity(body["repository"])
                ordinal = repo.one(
                    "SELECT max(ordinal)+1 AS n FROM project_versions WHERE project_id=%s", (pid,)
                )["n"]
                repo.execute(
                    "INSERT INTO project_versions(id,workspace_id,project_id,ordinal,repository,"
                    "base_ref,spec,spec_digest) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        pver,
                        project["workspace_id"],
                        pid,
                        ordinal,
                        repository,
                        body.get("base_ref", "main"),
                        spec,
                        digest(spec),
                    ),
                )
                repo.execute(
                    "UPDATE projects SET current_version_id=%s,version=version+1 WHERE id=%s",
                    (pver, pid),
                )
                return {"project_version_id": pver, "version": project["version"] + 1}

            return command(
                repo,
                principal,
                project["workspace_id"],
                "project.publish",
                key,
                {"project_id": pid, **body},
                perform,
            )
