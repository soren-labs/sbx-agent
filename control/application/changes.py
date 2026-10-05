from control.application.access import owned
from control.application.deduplication import command
from control.domain.errors import require
from control.domain.identity import new_id


class Changes:
    def __init__(self, uow, objects):
        self.uow, self.objects = uow, objects

    def capture(self, principal, sid, body, key):
        with self.uow.transaction() as repo:
            session = owned(repo, "sessions", sid, principal, lock=True)
            wt = repo.one("SELECT * FROM worktrees WHERE session_id=%s FOR UPDATE", (sid,))

            def perform():
                require(wt["generation"] == body["generation"], "version_conflict")
                origin = body.get("origin", "explicit")
                require(origin in {"automatic", "explicit", "salvage"}, "capture_failed")
                tid = body.get("source_turn_id")
                turn = owned(repo, "turns", tid, principal) if tid else None
                require(not turn or turn["session_id"] == sid, "not_found")
                eligible = bool(
                    turn
                    and turn["state"] == "succeeded"
                    and turn["evidence_complete"]
                    and origin != "salvage"
                )
                require(origin != "automatic" or eligible, "capture_failed")
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
                cs = new_id("cs")
                repo.execute(
                    "INSERT INTO worktree_operations(id,workspace_id,worktree_id,kind,"
                    "expected_generation,fence) VALUES(%s,%s,%s,'capture',%s,%s)",
                    (cs, session["workspace_id"], wt["id"], wt["generation"], wt["generation"]),
                )
                repo.execute(
                    "INSERT INTO changesets(id,workspace_id,session_id,worktree_id,source_turn_id,"
                    "generation,origin,automatic_eligible,base_sha) VALUES(%s,%s,%s,%s"
                    ",%s,%s,%s,%s,%s)",
                    (
                        cs,
                        session["workspace_id"],
                        sid,
                        wt["id"],
                        tid,
                        wt["generation"],
                        origin,
                        eligible,
                        wt["base_sha"],
                    ),
                )
                job = repo.enqueue(session["workspace_id"], "changeset.capture", cs, cs)
                seq = repo.event(
                    session["workspace_id"],
                    sid,
                    "changeset.capture_requested",
                    {"changeset_id": cs},
                )
                return {"changeset_id": cs, "job_id": job, "event_watermark": seq}

            return command(
                repo,
                principal,
                session["workspace_id"],
                "changeset.capture",
                key,
                {"session_id": sid, **body},
                perform,
            )

    def get(self, principal, cid):
        with self.uow.transaction() as repo:
            return owned(repo, "changesets", cid, principal)
