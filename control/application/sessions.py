from control.application.access import owned, workspace
from control.application.deduplication import command
from control.domain.errors import require
from control.domain.identity import new_id
from control.domain.sessions import TURN_TERMINAL


class Sessions:
    def __init__(self, uow):
        self.uow = uow

    def create(self, principal, workspace_id, body, key):
        workspace(principal, workspace_id)
        with self.uow.transaction() as repo:
            return command(
                repo,
                principal,
                workspace_id,
                "session.create",
                key,
                body,
                lambda: self.create_in(repo, principal, workspace_id, body),
            )

    def create_in(self, repo, principal, workspace_id, body):
        sid, wid = new_id("sess"), new_id("wt")
        provider = body.get("provider_id", "opencode")
        require(provider in {"opencode", "codex"}, "unsupported_capability")
        pver = body.get("project_version_id")
        if pver:
            owned(repo, "project_versions", pver, principal)
        repo.execute(
            "INSERT INTO sessions(id,workspace_id,creator_id,title,role,provider_id,"
            "project_version_id,effective_inputs) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                sid,
                workspace_id,
                principal.user_id,
                body.get("title", "New session"),
                body.get("role", "developer"),
                provider,
                pver,
                body,
            ),
        )
        repo.execute(
            "INSERT INTO worktrees(id,workspace_id,session_id) VALUES(%s,%s,%s)",
            (wid, workspace_id, sid),
        )
        seq = repo.event(
            workspace_id, sid, "session.created", {"role": body.get("role", "developer")}
        )
        return {"session_id": sid, "worktree_id": wid, "event_watermark": seq}

    def send(self, principal, sid, body, key):
        with self.uow.transaction() as repo:
            session = owned(repo, "sessions", sid, principal, lock=True)
            return command(
                repo,
                principal,
                session["workspace_id"],
                "message.send",
                key,
                {"session_id": sid, **body},
                lambda: self.send_in(repo, session, principal, body),
            )

    def send_in(self, repo, session, principal, body):
        require(session["lifecycle"] == "open", "version_conflict")
        routing = body.get("routing", "queue")
        require(routing in {"queue", "note"}, "unsupported_capability")
        content = body.get("content", "")
        require(isinstance(content, str) and 0 < len(content) <= 100_000, "output_contract_invalid")
        sid, ws = session["id"], session["workspace_id"]
        mid = new_id("msg")
        ords = repo.one(
            "UPDATE sessions SET next_message_ordinal=next_message_ordinal+1 "
            "WHERE id=%s RETURNING next_message_ordinal-1 AS ordinal",
            (sid,),
        )
        repo.execute(
            "INSERT INTO messages(id,workspace_id,session_id,ordinal,author_id,routing,content) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (mid, ws, sid, ords["ordinal"], principal.user_id, routing, content),
        )
        seq = repo.event(ws, sid, "message.accepted", {"message_id": mid, "routing": routing})
        tid, job = None, None
        if routing == "queue":
            tid = new_id("turn")
            ordinal = repo.one(
                "UPDATE sessions SET next_turn_ordinal=next_turn_ordinal+1 "
                "WHERE id=%s RETURNING next_turn_ordinal-1 AS ordinal",
                (sid,),
            )["ordinal"]
            repo.execute(
                "INSERT INTO turns(id,workspace_id,session_id,message_id,ordinal,settings) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (tid, ws, sid, mid, ordinal, body.get("settings", {})),
            )
            seq = repo.event(ws, sid, "turn.queued", {"turn_id": tid}, turn_id=tid)
            job = repo.enqueue(ws, "turn.dispatch", tid, tid)
        return {"message_id": mid, "turn_id": tid, "job_id": job, "event_watermark": seq}

    def cancel(self, principal, tid, key):
        with self.uow.transaction() as repo:
            turn = owned(repo, "turns", tid, principal)
            owned(repo, "sessions", turn["session_id"], principal, lock=True)
            turn = owned(repo, "turns", tid, principal, lock=True)

            def perform():
                if turn["state"] not in TURN_TERMINAL:
                    state = "cancelled" if turn["state"] == "queued" else "cancelling"
                    repo.execute(
                        "UPDATE turns SET state=%s,cancel_requested=true WHERE id=%s", (state, tid)
                    )
                    if state == "cancelled":
                        repo.execute(
                            "UPDATE jobs SET state='cancelled' WHERE turn_id=%s "
                            "AND state IN ('queued','retry_wait')",
                            (tid,),
                        )
                    repo.event(
                        turn["workspace_id"],
                        turn["session_id"],
                        "turn.cancelled" if state == "cancelled" else "turn.cancel_requested",
                        {"turn_id": tid},
                        turn_id=tid,
                    )
                return {
                    "turn_id": tid,
                    "state": turn["state"]
                    if turn["state"] in TURN_TERMINAL
                    else ("cancelled" if turn["state"] == "queued" else "cancelling"),
                }

            return command(
                repo, principal, turn["workspace_id"], "turn.cancel", key, {"turn_id": tid}, perform
            )

    def get(self, principal, sid):
        with self.uow.transaction() as repo:
            repo.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            session = owned(repo, "sessions", sid, principal)
            session["event_watermark"] = session["next_event_seq"] - 1
            session["turns"] = repo.all(
                "SELECT * FROM turns WHERE session_id=%s ORDER BY ordinal", (sid,)
            )
            session["messages"] = repo.all(
                "SELECT * FROM messages WHERE session_id=%s ORDER BY ordinal", (sid,)
            )
            return session
