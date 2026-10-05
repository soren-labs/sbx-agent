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
