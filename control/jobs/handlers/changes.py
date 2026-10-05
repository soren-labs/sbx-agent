import base64
import hashlib

from control.domain.changes import subject_digest
from control.domain.errors import DomainError, require
from control.runtime_client.grants import runtime_token


class CaptureHandler:
    def __init__(self, uow, claims, executor_factory, objects, master):
        self.uow, self.claims, self.executor_factory = uow, claims, executor_factory
        self.objects, self.master = objects, master

    def __call__(self, claim):
        try:
            self.run(claim)
        except DomainError as error:
            if error.code not in {"waiting_capacity", "executor_unavailable"}:
                with self.uow.transaction() as repo:
                    self.claims.assert_current(repo, claim)
                    repo.execute(
                        "UPDATE changesets SET state='failed' WHERE id=%s AND state!='ready'",
                        (claim.row["changeset_id"],),
                    )
                    repo.execute(
                        "UPDATE worktree_operations SET state='failed' WHERE id=%s",
                        (claim.row["changeset_id"],),
                    )
            raise

    def run(self, claim):
        with self.uow.transaction() as repo:
            cs = repo.one("SELECT * FROM changesets WHERE id=%s", (claim.row["changeset_id"],))
            if cs["state"] == "ready":
                return
            session = repo.one("SELECT * FROM sessions WHERE id=%s", (cs["session_id"],))
            wt = repo.one("SELECT * FROM worktrees WHERE id=%s", (cs["worktree_id"],))
            lease = repo.one(
                "SELECT * FROM executor_leases WHERE session_id=%s AND state='ready'",
                (session["id"],),
            )
            require(lease is not None, "executor_unavailable")
            require(
                not repo.one(
                    "SELECT id FROM turns WHERE session_id=%s AND state IN ('preparing"
                    "','running','cancelling')",
                    (session["id"],),
                ),
                "waiting_capacity",
            )
        token = runtime_token(self.master, lease["id"], lease["generation"])
        client = self.executor_factory(session, lease, token).connect_runtime(lease["handle"])
        client.submit(
            claim.row["effect_id"],
            "changes.capture",
            {
                "generation": cs["generation"],
                "base_sha": wt["base_sha"],
                "repository": wt["repository"],
            },
        )
        captured = client.wait(claim.row["effect_id"])
        require("subject" in captured, captured.get("error", "capture_failed"))
        subject = captured["subject"]
        require(subject_digest(subject) == captured["subject_digest"], "capture_failed")
        blobs = {}
        for entry in subject["files"]:
            if entry["type"] == "deleted":
                continue
            body = base64.b64decode(captured["contents"][entry["path"]], validate=True)
            require(hashlib.sha256(body).hexdigest() == entry["content_digest"], "capture_failed")
            blobs[entry["path"]] = self.objects.put(session["workspace_id"], body)
        with self.uow.transaction() as repo:
            repo.one("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (session["id"],))
            self.claims.assert_current(repo, claim)
            actual = repo.one(
                "SELECT generation FROM worktrees WHERE id=%s FOR UPDATE", (wt["id"],)
            )["generation"]
            require(actual == cs["generation"] == captured["generation"], "version_conflict")
            repo.execute(
                "UPDATE changesets SET state='ready',manifest=%s,subject_digest=%s,hea"
                "d_sha=%s,tree_sha=%s WHERE id=%s",
                (
                    {"subject": subject, "blobs": blobs},
                    captured["subject_digest"],
                    subject["head_sha"],
                    subject["tree_sha"],
                    cs["id"],
                ),
            )
            repo.execute(
                "UPDATE worktree_operations SET state='succeeded' WHERE id=%s", (cs["id"],)
            )
            for entry in subject["files"]:
                repo.execute(
                    "INSERT INTO changeset_files(changeset_id,path,mode,type,content_d"
                    "igest,blob_ref) "
                    "VALUES(%s,%s,%s,%s,%s,%s)",
                    (
                        cs["id"],
                        entry["path"],
                        entry["mode"],
                        entry["type"],
                        entry["content_digest"],
                        blobs.get(entry["path"]),
                    ),
                )
            repo.event(
                session["workspace_id"],
                session["id"],
                "changeset.ready",
                {"changeset_id": cs["id"], "subject_digest": captured["subject_digest"]},
            )
