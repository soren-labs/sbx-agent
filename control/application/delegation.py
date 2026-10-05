from datetime import UTC, datetime, timedelta

from control.application.access import owned
from control.application.deduplication import command
from control.domain.delegation import contract_digest, result_contract
from control.domain.errors import require
from control.domain.events import canonical
from control.domain.identity import new_id


class Delegations:
    def __init__(self, uow, sessions):
        self.uow, self.sessions = uow, sessions

    def spawn(self, principal, sid, body, key):
        with self.uow.transaction() as repo:
            parent = owned(repo, "sessions", sid, principal, lock=True)
            cs = owned(repo, "changesets", body["changeset_id"], principal)

            def perform():
                require(cs["state"] == "ready", "capture_failed")
                depth = repo.one(
                    "WITH RECURSIVE ancestors AS (SELECT parent_session_id FROM delega"
                    "tions WHERE child_session_id=%s "
                    "UNION ALL SELECT d.parent_session_id FROM delegations d JOIN ance"
                    "stors a ON d.child_session_id=a.parent_session_id) "
                    "SELECT count(*) AS n FROM ancestors",
                    (sid,),
                )["n"]
                require(depth < 3, "quota_exhausted")
                require(
                    repo.one(
                        "SELECT count(*) AS n FROM delegations WHERE parent_session_id=%s", (sid,)
                    )["n"]
                    < 8,
                    "quota_exhausted",
                )
                role = body.get("role", "review")
                contract = result_contract(role, cs["subject_digest"], cs["head_sha"])
                inputs = {
                    **parent["effective_inputs"],
                    "role": role,
                    "title": role.title() + " child",
                    "base_sha": cs["base_sha"],
                    "input_changeset_id": cs["id"],
                    "result_contract": contract,
                }
                inputs.update({k: body[k] for k in ("provider_id", "model") if k in body})
                child = self.sessions.create_in(repo, principal, parent["workspace_id"], inputs)
                child_row = repo.one(
                    "SELECT * FROM sessions WHERE id=%s FOR UPDATE", (child["session_id"],)
                )
                did = new_id("del")
                repo.execute(
                    "INSERT INTO delegations(id,workspace_id,parent_session_id,child_s"
                    "ession_id,changeset_id,"
                    "subject_digest,head_sha,role,contract,contract_digest,budget) VAL"
                    "UES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        did,
                        parent["workspace_id"],
                        sid,
                        child["session_id"],
                        cs["id"],
                        cs["subject_digest"],
                        cs["head_sha"],
                        role,
                        contract,
                        contract_digest(contract),
                        {"depth": 3, "children": 8, "deadline_seconds": 600},
                    ),
                )
                prompt = (
                    f"You are an independent {role} child. "
                    "Examine the exact applied immutable subject. "
                    "Use official CLI tools to inspect files and run relevant tests. N"
                    "ever push or modify parent work. "
                    "Respond with ONLY JSON matching this ResultContract: "
                    + canonical(contract)
                    + ". Include kind, subject_digest, head_sha, verdict, "
                    "findings (list of message/severity/path), "
                    "checks (list of name/status/command/evidence). Empty lists are al"
                    "lowed when justified. "
                    "A review approval must reflect actual inspection; a test pass mus"
                    "t reflect actual test exit evidence. " + body.get("summary", "")
                )
                accepted = self.sessions.send_in(repo, child_row, principal, {"content": prompt})
                repo.execute(
                    "UPDATE turns SET result_contract=%s WHERE id=%s",
                    (contract, accepted["turn_id"]),
                )
                seq = repo.event(
                    parent["workspace_id"],
                    sid,
                    "delegation.created",
                    {
                        "delegation_id": did,
                        "child_session_id": child["session_id"],
                        "subject_digest": cs["subject_digest"],
                    },
                )
                return {
                    "delegation_id": did,
                    "child_session_id": child["session_id"],
                    "turn_id": accepted["turn_id"],
                    "job_id": accepted["job_id"],
                    "event_watermark": seq,
                }

            return command(
                repo,
                principal,
                parent["workspace_id"],
                "delegation.spawn",
                key,
                {"parent_session_id": sid, **body},
                perform,
            )

    def wait(self, principal, did, key, seconds=600):
        with self.uow.transaction() as repo:
            delegation = owned(repo, "delegations", did, principal, lock=True)

            def perform():
                subscription = new_id("wait")
                result = repo.one(
                    "SELECT id FROM delegation_results WHERE delegation_id=%s", (did,)
                )
                state = "satisfied" if result else "pending"
                repo.execute(
                    "INSERT INTO wait_subscriptions(id,workspace_id,delegation_id,pare"
                    "nt_session_id,state,expires_at,evidence_result_id) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        subscription,
                        delegation["workspace_id"],
                        did,
                        delegation["parent_session_id"],
                        state,
                        datetime.now(UTC) + timedelta(seconds=min(seconds, 3600)),
                        result["id"] if result else None,
                    ),
                )
                return {
                    "subscription_id": subscription,
                    "state": state,
                    "result_id": result["id"] if result else None,
                }

            return command(
                repo,
                principal,
                delegation["workspace_id"],
                "delegation.wait",
                key,
                {"delegation_id": did, "seconds": seconds},
                perform,
            )

    def publish(self, principal, did, turn_id, key):
        with self.uow.transaction() as repo:
            row = owned(repo, "delegations", did, principal)
            turn = owned(repo, "turns", turn_id, principal)
            require(turn["session_id"] == row["child_session_id"], "not_found")

            def perform():
                job = repo.enqueue(
                    row["workspace_id"], "delegation.publish_result", did, did + "-result"
                )
                return {"delegation_id": did, "job_id": job}

            return command(
                repo,
                principal,
                row["workspace_id"],
                "delegation.publish",
                key,
                {"delegation_id": did, "turn_id": turn_id},
                perform,
            )

    def get(self, principal, did):
        with self.uow.transaction() as repo:
            row = owned(repo, "delegations", did, principal)
            row["result"] = repo.one(
                "SELECT * FROM delegation_results WHERE delegation_id=%s", (did,)
            )
            return row

    def message(self, principal, did, content, key):
        row = self.get(principal, did)
        return self.sessions.send(principal, row["child_session_id"], {"content": content}, key)

    def cancel(self, principal, did, key):
        row = self.get(principal, did)
        with self.uow.transaction() as repo:
            turns = repo.all(
                "SELECT id FROM turns WHERE session_id=%s "
                "AND state IN ('queued','preparing','running','cancelling')",
                (row["child_session_id"],),
            )
        for turn in turns:
            self.sessions.cancel(principal, turn["id"], key + ":" + turn["id"])
        with self.uow.transaction() as repo:
            repo.execute(
                "UPDATE delegations SET state='cancelled' WHERE id=%s AND state='active'", (did,)
            )
            repo.event(
                row["workspace_id"],
                row["parent_session_id"],
                "delegation.cancelled",
                {"delegation_id": did},
            )
        return {"delegation_id": did, "state": "cancelled"}
