from control.domain.errors import require
from control.domain.events import digest
from control.domain.identity import new_id
from control.domain.sessions import TURN_TERMINAL, terminal_verdict


class Ingest:
    def __init__(self, uow, claims):
        self.uow, self.claims = uow, claims

    def batch(self, claim, execution_id, epoch, events):
        with self.uow.transaction() as repo:
            execution = repo.one("SELECT * FROM executions WHERE id=%s", (execution_id,))
            turn = repo.one("SELECT * FROM turns WHERE id=%s", (execution["turn_id"],))
            session = repo.one(
                "SELECT * FROM sessions WHERE id=%s FOR UPDATE", (turn["session_id"],)
            )
            lease = repo.one(
                "SELECT * FROM executor_leases WHERE id=%s FOR UPDATE", (execution["lease_id"],)
            )
            self.claims.assert_current(repo, claim)
            require(lease["state"] == "ready", "version_conflict")
            repo.execute(
                "INSERT INTO runtime_ingestion_offsets(lease_id,runtime_epoch) VALUES(%s,%s) "
                "ON CONFLICT DO NOTHING",
                (lease["id"], epoch),
            )
            offset = repo.one(
                "SELECT * FROM runtime_ingestion_offsets WHERE lease_id=%s AND "
                "runtime_epoch=%s FOR UPDATE",
                (lease["id"], epoch),
            )
            ack = offset["ack"]
            for event in events:
                seq = event["local_seq"]
                if seq <= ack:
                    previous = repo.one(
                        "SELECT type,payload FROM session_events WHERE "
                        "executor_lease_id=%s AND runtime_epoch=%s AND local_seq=%s",
                        (lease["id"], epoch, seq),
                    )
                    require(
                        previous
                        and previous["type"] == event["type"]
                        and digest(previous["payload"]) == digest(event["payload"]),
                        "idempotency_conflict",
                    )
                    continue
                require(seq == ack + 1, "invalid_cursor")
                # Batches include all operations in a lease; only bound execution evidence
                # may be projected, otherwise fail instead of misattributing a frame.
                require(event["operation_id"] == execution["operation_id"], "version_conflict")
                payload = event["payload"]
                repo.event(
                    session["workspace_id"],
                    session["id"],
                    event["type"],
                    payload,
                    turn_id=turn["id"],
                    execution_id=execution_id,
                    executor_lease_id=lease["id"],
                    runtime_epoch=epoch,
                    local_seq=seq,
                    source={"kind": "runtime", "runtime_epoch": epoch, "local_seq": seq},
                )
                if event["type"] == "execution.native_bound":
                    old = repo.one(
                        "SELECT * FROM native_context_bindings WHERE session_id=%s "
                        "ORDER BY created_at DESC LIMIT 1",
                        (session["id"],),
                    )
                    require(not old or old["native_id"] == payload["native_id"], "context_mismatch")
                    if not old:
                        repo.execute(
                            "INSERT INTO native_context_bindings(id,workspace_id,sessi"
                            "on_id,provider_id,native_id,"
                            "lineage_id,cli_version,adapter_version,account_connection"
                            "_id,account_credential_id) "
                            "VALUES(%s,%s,%s,%s,%s,%s,'1.18.29','1',%s,%s)",
                            (
                                new_id("native"),
                                session["workspace_id"],
                                session["id"],
                                session["provider_id"],
                                payload["native_id"],
                                new_id("lineage"),
                                session["zen_connection_id"],
                                execution["credential_id"],
                            ),
                        )
                if event["type"] == "message.part_updated":
                    repo.execute(
                        "INSERT INTO message_parts(id,workspace_id,session_id,execution_id,"
                        "provider_part_id,kind,revision,content) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT(execution_id,provider_part_id) DO UPDATE SET revi"
                        "sion=excluded.revision,"
                        "content=excluded.content WHERE message_parts.revision<excluded.revision",
                        (
                            new_id("part"),
                            session["workspace_id"],
                            session["id"],
                            execution_id,
                            payload["part_id"],
                            payload["kind"],
                            payload["revision"],
                            payload["text"],
                        ),
                    )
                ack = seq
            repo.execute(
                "UPDATE runtime_ingestion_offsets SET ack=%s WHERE lease_id=%s AND run"
                "time_epoch=%s",
                (ack, lease["id"], epoch),
            )
            return ack

    def finish(self, claim, execution_id, epoch, result):
        with self.uow.transaction() as repo:
            execution = repo.one("SELECT * FROM executions WHERE id=%s", (execution_id,))
            turn = repo.one("SELECT * FROM turns WHERE id=%s", (execution["turn_id"],))
            session = repo.one(
                "SELECT * FROM sessions WHERE id=%s FOR UPDATE", (turn["session_id"],)
            )
            turn = repo.one("SELECT * FROM turns WHERE id=%s FOR UPDATE", (turn["id"],))
            self.claims.assert_current(repo, claim)
            if turn["state"] in TURN_TERMINAL:
                return turn["state"]
            offset = repo.one(
                "SELECT ack FROM runtime_ingestion_offsets WHERE lease_id=%s AND runtime_epoch=%s",
                (execution["lease_id"], epoch),
            )
            final = result.get("final_watermark")
            complete = final is not None and offset and offset["ack"] >= final
            verdict = terminal_verdict(
                turn["state"],
                stopped=result.get("stopped", False),
                success=result.get("outcome") == "success",
                complete=bool(complete),
            )
            # Stop and complete evidence precede terminalization; cancellation wins.
            repo.execute(
                "UPDATE turns SET state=%s,evidence_complete=%s,outcome=%s,reason=%s WHERE id=%s",
                (verdict, bool(complete), result, result.get("error"), turn["id"]),
            )
            estate = {
                "succeeded": "succeeded",
                "failed": "failed",
                "cancelled": "cancelled",
                "interrupted": "unknown",
            }[verdict]
            if result.get("stopped"):
                repo.execute(
                    "UPDATE capacity_reservations SET state='released' WHERE execution_id=%s",
                    (execution_id,),
                )
                repo.execute(
                    "UPDATE executions SET isolation_confirmed=true WHERE id=%s", (execution_id,)
                )
            repo.execute(
                "UPDATE executions SET state=%s,final_watermark=%s,outcome=%s,native_id=%s "
                "WHERE id=%s",
                (estate, final, result, result.get("native_id"), execution_id),
            )
            if result.get("native_id") and verdict == "succeeded":
                old = repo.one(
                    "SELECT * FROM native_context_bindings WHERE session_id=%s ORDER B"
                    "Y created_at DESC LIMIT 1",
                    (session["id"],),
                )
                require(not old or old["native_id"] == result["native_id"], "context_mismatch")
                if not old:
                    repo.execute(
                        "INSERT INTO native_context_bindings(id,workspace_id,session_i"
                        "d,provider_id,"
                        "native_id,lineage_id,cli_version,adapter_version) VALUES(%s,%"
                        "s,%s,%s,%s,%s,%s,%s)",
                        (
                            new_id("native"),
                            session["workspace_id"],
                            session["id"],
                            session["provider_id"],
                            result["native_id"],
                            new_id("lineage"),
                            "1.18.29",
                            "1",
                        ),
                    )
            if result.get("native_id") and verdict == "succeeded":
                repo.execute(
                    "UPDATE native_context_bindings SET account_connection_id=%s,accou"
                    "nt_credential_id=%s "
                    "WHERE session_id=%s AND account_credential_id IS NULL",
                    (session["zen_connection_id"], execution["credential_id"], session["id"]),
                )
            repo.event(
                session["workspace_id"],
                session["id"],
                "turn." + verdict,
                {"turn_id": turn["id"], "evidence_complete": bool(complete)},
                turn_id=turn["id"],
            )
            repo.execute(
                "UPDATE worktrees SET generation=generation+1 WHERE session_id=%s", (session["id"],)
            )
            return verdict
