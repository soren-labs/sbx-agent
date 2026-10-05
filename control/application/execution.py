from datetime import UTC, datetime, timedelta

from control.domain.errors import require
from control.domain.identity import new_id


class Execution:
    def __init__(self, uow, claims):
        self.uow, self.claims = uow, claims

    def admit(self, claim):
        with self.uow.transaction() as repo:
            turn = repo.one("SELECT * FROM turns WHERE id=%s", (claim.row["turn_id"],))
            session = repo.one(
                "SELECT * FROM sessions WHERE id=%s FOR UPDATE", (turn["session_id"],)
            )
            self.claims.assert_current(repo, claim)
            turn = repo.one("SELECT * FROM turns WHERE id=%s FOR UPDATE", (turn["id"],))
            require(session["lifecycle"] == "open", "version_conflict")
            existing = repo.one(
                "SELECT * FROM executions WHERE turn_id=%s ORDER BY attempt_ordinal DESC LIMIT 1",
                (turn["id"],),
            )
            if existing:
                lease = repo.one(
                    "SELECT * FROM executor_leases WHERE id=%s", (existing["lease_id"],)
                )
                return session, turn, existing, lease
            require(turn["state"] == "queued", "version_conflict")
            require(
                not repo.one(
                    "SELECT id FROM turns WHERE session_id=%s AND (state IN "
                    "('preparing','running','cancelling') OR (state='queued' AND ordinal<%s))",
                    (session["id"], turn["ordinal"]),
                ),
                "waiting_capacity",
            )
            # Unknown outcomes and unverified lost compute prohibit new writers.
            require(
                not repo.one(
                    "SELECT e.id FROM executions e JOIN turns t ON t.id=e.turn_id "
                    "WHERE t.session_id=%s AND e.state='unknown'",
                    (session["id"],),
                ),
                "outcome_unknown",
            )
            lease = repo.one(
                "SELECT * FROM executor_leases WHERE session_id=%s AND state IN "
                "('allocating','ready','quiescing') FOR UPDATE",
                (session["id"],),
            )
            if not lease:
                previous = repo.one(
                    "SELECT coalesce(max(generation),0)+1 AS gen FROM executor_leases "
                    "WHERE session_id=%s",
                    (session["id"],),
                )
                inputs = session["effective_inputs"]
                lid = new_id("lease")
                repo.execute(
                    "INSERT INTO executor_leases(id,workspace_id,session_id,backend,generation,"
                    "allocation_operation_id,connection_id,expires_at) VALUES(%s,%s,%s"
                    ",%s,%s,%s,%s,%s)",
                    (
                        lid,
                        session["workspace_id"],
                        session["id"],
                        inputs.get("backend", "modal"),
                        previous["gen"],
                        new_id("allocation"),
                        inputs.get("modal_connection_id"),
                        datetime.now(UTC) + timedelta(minutes=30),
                    ),
                )
                lease = repo.one("SELECT * FROM executor_leases WHERE id=%s", (lid,))
            eid = new_id("exec")
            repo.execute(
                "INSERT INTO executions(id,workspace_id,turn_id,lease_id,attempt_ordin"
                "al,operation_id) "
                "VALUES(%s,%s,%s,%s,1,%s)",
                (eid, session["workspace_id"], turn["id"], lease["id"], new_id("op")),
            )
            repo.execute("UPDATE turns SET state='preparing' WHERE id=%s", (turn["id"],))
            repo.event(
                session["workspace_id"],
                session["id"],
                "turn.preparing",
                {"turn_id": turn["id"], "execution_id": eid},
                turn_id=turn["id"],
                execution_id=eid,
            )
            return session, turn, repo.one("SELECT * FROM executions WHERE id=%s", (eid,)), lease

    def bind(self, claim, session, lease, handle, hello):
        with self.uow.transaction() as repo:
            repo.one("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (session["id"],))
            self.claims.assert_current(repo, claim)
            require(
                hello["lease_id"] == lease["id"]
                and hello["lease_generation"] == lease["generation"],
                "version_conflict",
            )
            repo.execute(
                "UPDATE executor_leases SET handle=%s,state='ready' WHERE id=%s AND st"
                "ate='allocating'",
                (handle, lease["id"]),
            )
            repo.event(
                session["workspace_id"],
                session["id"],
                "executor.bound",
                {"lease_id": lease["id"], "generation": lease["generation"]},
                executor_lease_id=lease["id"],
            )

    def started(self, claim, session, turn, execution):
        with self.uow.transaction() as repo:
            repo.one("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (session["id"],))
            self.claims.assert_current(repo, claim)
            repo.execute(
                "UPDATE executions SET state='started' WHERE id=%s AND state='preparing'",
                (execution["id"],),
            )
            row = repo.one("SELECT state FROM turns WHERE id=%s", (turn["id"],))
            if row["state"] == "preparing":
                repo.execute("UPDATE turns SET state='running' WHERE id=%s", (turn["id"],))
                repo.event(
                    session["workspace_id"],
                    session["id"],
                    "turn.started",
                    {"turn_id": turn["id"]},
                    turn_id=turn["id"],
                )

    def lost(self, claim, execution_id, reason="executor_unavailable"):
        with self.uow.transaction() as repo:
            execution = repo.one("SELECT * FROM executions WHERE id=%s", (execution_id,))
            turn = repo.one("SELECT * FROM turns WHERE id=%s", (execution["turn_id"],))
            repo.one("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (turn["session_id"],))
            self.claims.assert_current(repo, claim)
            if turn["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                return
            # Durable unknown outcome and lost/quarantined compute are separate.
            # Unknown Execution blocks further dispatch until isolation + acknowledgement.
            if turn["state"] == "preparing":
                repo.execute("UPDATE turns SET state='running' WHERE id=%s", (turn["id"],))
            repo.execute(
                "UPDATE turns SET state='interrupted',reason=%s WHERE id=%s", (reason, turn["id"])
            )
            repo.execute("UPDATE executions SET state='unknown' WHERE id=%s", (execution_id,))
            repo.execute(
                "UPDATE executor_leases SET state='lost' WHERE id=%s", (execution["lease_id"],)
            )
            repo.execute(
                "UPDATE worktrees SET availability='unavailable' WHERE session_id=%s",
                (turn["session_id"],),
            )
            repo.event(
                turn["workspace_id"],
                turn["session_id"],
                "executor.unavailable",
                {"lease_id": execution["lease_id"], "quarantined": True},
                executor_lease_id=execution["lease_id"],
            )
            repo.event(
                turn["workspace_id"],
                turn["session_id"],
                "turn.interrupted",
                {"turn_id": turn["id"], "reason": reason},
                turn_id=turn["id"],
            )
