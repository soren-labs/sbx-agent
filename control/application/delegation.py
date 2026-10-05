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
                require(1 <= body.get("budget_seconds", 900) <= 1800, "quota_exhausted")
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
                        {
                            "depth": 3,
                            "children": 8,
                            "deadline_seconds": body.get("budget_seconds", 900),
                        },
                    ),
                )
                timeout_job = repo.enqueue(
                    parent["workspace_id"], "delegation.expire", did, did + "-deadline"
                )
                repo.execute(
                    "UPDATE jobs SET due_at=now()+%s*interval '1 second' WHERE id=%s",
                    (body.get("budget_seconds", 900), timeout_job),
                )
                template = {
                    "kind": contract["kind"],
                    "subject_digest": cs["subject_digest"],
                    "head_sha": cs["head_sha"],
                    "verdict": "choose an allowed verdict from actual evidence",
                    "findings": [],
                    "checks": [],
                }
                prompt = (
                    f"You are an independent {role} child. "
                    "Examine the exact applied immutable subject. "
                    "Use official CLI tools to inspect files and run relevant tests. N"
                    "ever push or modify parent work. "
                    "Respond with ONLY JSON matching this ResultContract: "
                    + canonical(contract)
                    + ". Copy kind, subject_digest and head_sha EXACTLY from this contract. "
                    "If head_sha is null, it MUST remain JSON null: Git HEAD is only "
                    "the baseline and MUST NOT replace the reviewed patch-only head. "
                    "The platform verifies the canonical subject digest; inspect the "
                    "applied files rather than inventing another digest algorithm. "
                    "Output template (choose verdict from your evidence): "
                    + canonical(template)
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
                if state == "pending":
                    job = repo.enqueue(
                        delegation["workspace_id"], "delegation.expire_wait", did, subscription
                    )
                    repo.execute(
                        "UPDATE jobs SET due_at=%s WHERE id=%s",
                        (datetime.now(UTC) + timedelta(seconds=min(max(seconds, 1), 3600)), job),
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
                    row["workspace_id"],
                    "delegation.publish_result",
                    did,
                    did + "-result-" + turn_id,
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
        with self.uow.transaction() as repo:
            row = owned(repo, "delegations", did, principal, lock=True)
            return command(
                repo,
                principal,
                row["workspace_id"],
                "delegation.cancel",
                key,
                {"delegation_id": did},
                lambda: self.cancel_tree_in(repo, row),
            )

    def cancel_tree_in(self, repo, root, *, reason="cancelled"):
        rows = repo.all(
            "WITH RECURSIVE tree AS (SELECT * FROM delegations WHERE id=%s UNION ALL S"
            "ELECT d.* FROM delegations d JOIN tree t ON d.parent_session_id=t.child_s"
            "ession_id) SELECT * FROM tree ORDER BY child_session_id",
            (root["id"],),
        )
        for row in rows:
            repo.one("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (row["child_session_id"],))
            current = repo.one("SELECT state FROM delegations WHERE id=%s FOR UPDATE", (row["id"],))
            if current["state"] != "active":
                continue
            turns = repo.all(
                "SELECT * FROM turns WHERE session_id=%s AND state IN ('queued','prepa"
                "ring','running','cancelling') ORDER BY ordinal FOR UPDATE",
                (row["child_session_id"],),
            )
            for turn in turns:
                self.sessions.cancel_in(repo, turn)
            repo.execute("UPDATE delegations SET state='cancelled' WHERE id=%s", (row["id"],))
            repo.execute(
                "UPDATE wait_subscriptions SET state='expired' WHERE delegation_id=%s "
                "AND state='pending'",
                (row["id"],),
            )
            repo.event(
                row["workspace_id"],
                row["parent_session_id"],
                "delegation.cancelled",
                {"delegation_id": row["id"], "reason": reason},
            )
        # No capacity or lease is freed before confirmed process isolation.
        return {"delegation_id": root["id"], "state": "cancelled"}
