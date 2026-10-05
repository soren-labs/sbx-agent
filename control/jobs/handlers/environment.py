from control.domain.errors import require
from control.domain.identity import Principal


class EnvironmentResolver:
    def __init__(self, uow, connections, github):
        self.uow, self.connections, self.github = uow, connections, github

    def __call__(self, session, execution, lease):
        with self.uow.transaction() as repo:
            wt = repo.one("SELECT * FROM worktrees WHERE session_id=%s", (session["id"],))
            pver = (
                repo.one(
                    "SELECT * FROM project_versions WHERE id=%s", (session["project_version_id"],)
                )
                if session["project_version_id"]
                else None
            )
        inputs = dict(session["effective_inputs"])
        if pver:
            inputs = {
                **pver["spec"],
                **inputs,
                "repository": pver["repository"],
                "base_ref": pver["base_ref"],
            }
        if wt["last_snapshot_id"] or lease.get("environment_prepared_at"):
            return {**inputs, "base_sha": wt["base_sha"]}, {}
        credential = {}
        repository = inputs.get("repository")
        if repository and session["github_connection_id"]:
            principal = Principal(session["creator_id"], (session["workspace_id"],))
            credential, version = self.connections.resolve(
                principal,
                session["github_connection_id"],
                "clone",
                session_id=session["id"],
                lease_id=lease["id"],
                operation_id=execution["operation_id"] + "-clone",
            )
            # Pin once before first CLI launch, never reset a later Turn to new main.
            inputs["base_sha"] = (
                wt["base_sha"]
                or inputs.get("base_sha")
                or self.github.resolve_base(credential, repository, inputs.get("base_ref", "main"))
            )
        require(not repository or inputs.get("base_sha"), "repository_unavailable")
        return inputs, credential
