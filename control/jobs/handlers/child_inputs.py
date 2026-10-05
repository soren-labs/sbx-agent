import base64

from control.domain.errors import require


class ChildInputs:
    def __init__(self, uow, objects, claims):
        self.uow, self.objects, self.claims = uow, objects, claims

    def __call__(self, session, execution, lease, client, claim):
        csid = session["effective_inputs"].get("input_changeset_id")
        if not csid:
            return
        with self.uow.transaction() as repo:
            cs = repo.one(
                "SELECT * FROM changesets WHERE id=%s AND workspace_id=%s AND state='ready'",
                (csid, session["workspace_id"]),
            )
            require(cs is not None, "not_found")
        contents = {
            path: base64.b64encode(self.objects.get(session["workspace_id"], key)).decode()
            for path, key in cs["manifest"]["blobs"].items()
        }
        operation = lease["allocation_operation_id"] + "-input"
        client.submit(
            operation,
            "changes.apply",
            {
                "generation": client.hello["worktree_generation"],
                "subject": cs["manifest"]["subject"],
                "subject_digest": cs["subject_digest"],
                "contents": contents,
            },
        )
        applied = client.wait(operation)
        require(applied.get("applied"), applied.get("error", "capture_failed"))
        with self.uow.transaction() as repo:
            self.claims.assert_current(repo, claim)
            repo.execute(
                "UPDATE worktrees SET generation=greatest(generation,%s) WHERE session_id=%s",
                (applied["generation"], session["id"]),
            )
