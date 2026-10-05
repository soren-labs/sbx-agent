from control.application.access import owned
from control.application.deduplication import command
from control.domain.errors import require
from control.domain.identity import new_id
from control.runtime_client.grants import runtime_token


class SessionIO:
    def __init__(self, uow, claims, executor_factory, master, objects):
        self.uow, self.claims, self.executor_factory = uow, claims, executor_factory
        self.master, self.objects = master, objects

    def client(self, principal, sid):
        with self.uow.transaction() as repo:
            session = owned(repo, "sessions", sid, principal)
            lease = repo.one(
                "SELECT * FROM executor_leases WHERE session_id=%s AND state='ready'", (sid,)
            )
            require(lease is not None, "executor_unavailable")
        token = runtime_token(self.master, lease["id"], lease["generation"])
        return self.executor_factory(session, {**lease, "_read_only": True}, token).connect_runtime(
            lease["handle"]
        )

    def files(self, principal, sid, path=None):
        client = self.client(principal, sid)
        return client.get("/files/read", path=path) if path else client.get("/files")

    def operation(self, principal, sid, kind, payload, key):
        with self.uow.transaction() as repo:
            session = owned(repo, "sessions", sid, principal, lock=True)
            wt = repo.one("SELECT * FROM worktrees WHERE session_id=%s FOR UPDATE", (sid,))

            def perform():
                require(
                    not repo.one(
                        "SELECT id FROM turns WHERE session_id=%s "
                        "AND state IN ('preparing','running','cancelling')",
                        (sid,),
                    ),
                    "waiting_capacity",
                )
                require(
                    not repo.one(
                        "SELECT id FROM worktree_operations WHERE worktree_id=%s "
                        "AND state IN ('pending','executing')",
                        (wt["id"],),
                    ),
                    "waiting_capacity",
                )
                if "generation" in payload:
                    require(payload["generation"] == wt["generation"], "version_conflict")
                lid = repo.one(
                    "SELECT id FROM executor_leases WHERE session_id=%s AND state='ready'", (sid,)
                )
                require(lid is not None, "executor_unavailable")
                op = new_id("io")
                repo.execute(
                    "INSERT INTO worktree_operations(id,workspace_id,worktree_id,kind,"
                    "expected_generation,fence,payload,lease_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        op,
                        session["workspace_id"],
                        wt["id"],
                        kind,
                        wt["generation"],
                        wt["generation"],
                        payload,
                        lid["id"],
                    ),
                )
                job = repo.enqueue(session["workspace_id"], "worktree.perform", wt["id"], op)
                seq = repo.event(
                    session["workspace_id"],
                    sid,
                    "worktree.operation_requested",
                    {"operation_id": op, "kind": kind},
                )
                return {"operation_id": op, "job_id": job, "event_watermark": seq}

            return command(
                repo,
                principal,
                session["workspace_id"],
                kind,
                key,
                {"session_id": sid, **payload},
                perform,
            )

    def apply(self, principal, csid, body, key):
        with self.uow.transaction() as repo:
            cs = owned(repo, "changesets", csid, principal)
            require(
                cs["state"] == "ready" and cs["subject_digest"] == body["subject_digest"],
                "stale_subject",
            )
        return self.operation(
            principal,
            body["session_id"],
            "changes.apply",
            {
                "generation": body["generation"],
                "changeset_id": csid,
                "subject_digest": body["subject_digest"],
            },
            key,
        )

    def close_terminal(self, principal, sid, operation_id, key):
        with self.uow.transaction() as repo:
            owned(repo, "sessions", sid, principal, lock=True)
            op = repo.one(
                "SELECT o.* FROM worktree_operations o JOIN worktrees w ON w.id=o.worktree_id "
                "WHERE o.id=%s AND w.session_id=%s FOR UPDATE OF o",
                (operation_id, sid),
            )
            require(op is not None and op["kind"] == "terminal.open", "not_found")
            job = repo.enqueue(
                op["workspace_id"],
                "worktree.terminal_close",
                op["worktree_id"],
                operation_id + "-close",
            )
            return {"job_id": job, "operation_id": operation_id}
