from control.application.access import owned
from control.application.deduplication import command
from control.domain.errors import require
from control.domain.identity import new_id


class Worktrees:
    def __init__(self, uow):
        self.uow = uow

    def checkpoint(self, principal, sid, generation, key):
        with self.uow.transaction() as repo:
            session = owned(repo, "sessions", sid, principal, lock=True)
            wt = repo.one("SELECT * FROM worktrees WHERE session_id=%s FOR UPDATE", (sid,))

            def perform():
                require(
                    not repo.one(
                        "SELECT id FROM service_instances WHERE session_id=%s AND state='running'",
                        (sid,),
                    ),
                    "waiting_capacity",
                )
                require(wt["generation"] == generation, "version_conflict")
                require(
                    not repo.one(
                        "SELECT id FROM worktree_operations WHERE worktree_id=%s "
                        "AND state IN ('pending','executing')",
                        (wt["id"],),
                    ),
                    "waiting_capacity",
                )
                require(
                    not repo.one(
                        "SELECT id FROM turns WHERE session_id=%s AND state IN "
                        "('preparing','running','cancelling')",
                        (sid,),
                    ),
                    "waiting_capacity",
                )
                snap = new_id("snap")
                repo.execute(
                    "INSERT INTO worktree_operations(id,workspace_id,worktree_id,kind,"
                    "expected_generation,fence) VALUES(%s,%s,%s,'snapshot',%s,%s)",
                    (snap, session["workspace_id"], wt["id"], generation, generation),
                )
                repo.execute(
                    "INSERT INTO snapshots(id,workspace_id,kind,worktree_id,generation) "
                    "VALUES(%s,%s,'checkpoint',%s,%s)",
                    (snap, session["workspace_id"], wt["id"], generation),
                )
                job = repo.enqueue(session["workspace_id"], "snapshot.capture", snap, snap)
                seq = repo.event(
                    session["workspace_id"], sid, "snapshot.requested", {"snapshot_id": snap}
                )
                return {"snapshot_id": snap, "job_id": job, "event_watermark": seq}

            return command(
                repo,
                principal,
                session["workspace_id"],
                "snapshot.capture",
                key,
                {"session_id": sid, "generation": generation},
                perform,
            )

    def release(self, principal, sid, key):
        with self.uow.transaction() as repo:
            session = owned(repo, "sessions", sid, principal, lock=True)

            def perform():
                require(
                    not repo.one(
                        "SELECT id FROM turns WHERE session_id=%s AND state IN ('prepa"
                        "ring','running','cancelling')",
                        (sid,),
                    ),
                    "waiting_capacity",
                )
                lease = repo.one(
                    "SELECT * FROM executor_leases WHERE session_id=%s AND cleanup_confirmed=false "
                    "ORDER BY generation DESC LIMIT 1 FOR UPDATE",
                    (sid,),
                )
                if not lease:
                    return {"released": True}
                if lease["state"] == "ready":
                    repo.execute(
                        "UPDATE executor_leases SET state='quiescing' WHERE id=%s", (lease["id"],)
                    )
                job = repo.enqueue(
                    session["workspace_id"],
                    "executor.release",
                    lease["id"],
                    lease["id"] + "-release",
                )
                repo.execute(
                    "UPDATE jobs SET state='queued',due_at=now() WHERE id=%s AND state='failed'",
                    (job,),
                )
                return {"lease_id": lease["id"], "job_id": job}

            return command(
                repo,
                principal,
                session["workspace_id"],
                "executor.release",
                key,
                {"session_id": sid},
                perform,
            )
