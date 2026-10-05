from control.application.access import owned
from control.application.deduplication import command
from control.domain.errors import require


class Lifecycle:
    def __init__(self, uow, sessions):
        self.uow, self.sessions = uow, sessions

    def change(self, principal, sid, action, body, key):
        with self.uow.transaction() as repo:
            row = owned(repo, "sessions", sid, principal, lock=True)

            def perform():
                require(row["version"] == body["expected_version"], "version_conflict")
                target = {"archive": "archived", "unarchive": "open", "close": "closed"}[action]
                require(
                    target
                    in {"open": {"archived", "closed"}, "archived": {"open", "closed"}}.get(
                        row["lifecycle"], set()
                    ),
                    "version_conflict",
                )
                require(
                    not repo.one(
                        "SELECT id FROM turns WHERE session_id=%s "
                        "AND state IN ('queued','preparing','running','cancelling')",
                        (sid,),
                    ),
                    "waiting_capacity",
                )
                repo.execute("UPDATE sessions SET lifecycle=%s WHERE id=%s", (target, sid))
                seq = repo.event(row["workspace_id"], sid, "session." + target, {})
                return {"session_id": sid, "lifecycle": target, "event_watermark": seq}

            return command(
                repo,
                principal,
                row["workspace_id"],
                "session." + action,
                key,
                {"session_id": sid, **body},
                perform,
            )

    def patch(self, principal, sid, body, key):
        with self.uow.transaction() as repo:
            row = owned(repo, "sessions", sid, principal, lock=True)

            def perform():
                require(row["version"] == body["expected_version"], "version_conflict")
                require(
                    isinstance(body.get("title"), str) and len(body["title"]) <= 200, "forbidden"
                )
                repo.execute("UPDATE sessions SET title=%s WHERE id=%s", (body["title"], sid))
                seq = repo.event(
                    row["workspace_id"], sid, "session.settings_changed", {"title": body["title"]}
                )
                return {"session_id": sid, "event_watermark": seq}

            return command(
                repo,
                principal,
                row["workspace_id"],
                "session.patch",
                key,
                {"session_id": sid, **body},
                perform,
            )

    def retry(self, principal, tid, key):
        with self.uow.transaction() as repo:
            turn = owned(repo, "turns", tid, principal)
            require(turn["state"] in {"failed", "cancelled", "interrupted"}, "version_conflict")
            require(turn["state"] != "interrupted", "outcome_unknown")
            content = repo.one("SELECT content FROM messages WHERE id=%s", (turn["message_id"],))[
                "content"
            ]
        return self.sessions.send(
            principal, turn["session_id"], {"content": content, "retry_of_turn_id": tid}, key
        )

    def acknowledge(self, principal, eid, key):
        with self.uow.transaction() as repo:
            execution = owned(repo, "executions", eid, principal)
            turn = owned(repo, "turns", execution["turn_id"], principal)
            session = owned(repo, "sessions", turn["session_id"], principal, lock=True)

            def perform():
                lease = repo.one(
                    "SELECT * FROM executor_leases WHERE id=%s FOR UPDATE", (execution["lease_id"],)
                )
                wt = repo.one("SELECT * FROM worktrees WHERE session_id=%s", (session["id"],))
                require(execution["state"] == "unknown", "version_conflict")
                require(lease["cleanup_confirmed"], "outcome_unknown")
                require(wt["last_snapshot_id"] is not None, "context_unavailable")
                repo.execute(
                    "UPDATE executions SET acknowledged_at=now() WHERE id=%s AND ackno"
                    "wledged_at IS NULL",
                    (eid,),
                )
                seq = repo.event(
                    session["workspace_id"],
                    session["id"],
                    "execution.recovery_acknowledged",
                    {
                        "execution_id": eid,
                        "snapshot_id": wt["last_snapshot_id"],
                        "outcome_remains_unknown": True,
                    },
                )
                return {
                    "execution_id": eid,
                    "acknowledged": True,
                    "snapshot_id": wt["last_snapshot_id"],
                    "event_watermark": seq,
                }

            return command(
                repo,
                principal,
                session["workspace_id"],
                "execution.acknowledge",
                key,
                {"execution_id": eid},
                perform,
            )

    def continuation(self, principal, sid, body, key):
        with self.uow.transaction() as repo:
            parent = owned(repo, "sessions", sid, principal, lock=True)

            def perform():
                require(
                    isinstance(body.get("summary"), str) and 0 < len(body["summary"]) <= 100000,
                    "invalid_request",
                )
                inputs = {
                    **parent["effective_inputs"],
                    "title": body.get("title", "Linked continuation"),
                }
                for old in ("result_contract", "input_changeset_id", "message", "native_id"):
                    inputs.pop(old, None)
                if body.get("changeset_id"):
                    cs = owned(repo, "changesets", body["changeset_id"], principal)
                    require(cs["state"] == "ready", "capture_failed")
                    inputs.update(input_changeset_id=cs["id"], base_sha=cs["base_sha"])
                inputs["role"] = "coding"
                child = self.sessions.create_in(repo, principal, parent["workspace_id"], inputs)
                repo.execute(
                    "UPDATE sessions SET linked_from_session_id=%s WHERE id=%s",
                    (sid, child["session_id"]),
                )
                row = repo.one(
                    "SELECT * FROM sessions WHERE id=%s FOR UPDATE", (child["session_id"],)
                )
                accepted = self.sessions.send_in(
                    repo,
                    row,
                    principal,
                    {
                        "content": "Explicit linked continuation; "
                        "this is a fresh native session. Summary: " + body["summary"]
                    },
                )
                repo.event(
                    parent["workspace_id"],
                    sid,
                    "session.continuation_created",
                    {"session_id": child["session_id"], "changeset_id": body.get("changeset_id")},
                )
                return {
                    **child,
                    **accepted,
                    "linked_from_session_id": sid,
                    "native_context": "fresh",
                }

            return command(
                repo,
                principal,
                parent["workspace_id"],
                "session.continuation",
                key,
                {"session_id": sid, **body},
                perform,
            )
