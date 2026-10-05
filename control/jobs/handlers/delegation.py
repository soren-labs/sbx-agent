from control.domain.delegation import validate_result
from control.domain.errors import DomainError, require
from control.domain.identity import Principal, new_id


class ResultHandler:
    def __init__(self, uow, claims, sessions):
        self.uow, self.claims, self.sessions = uow, claims, sessions

    def __call__(self, claim):
        with self.uow.transaction() as repo:
            delegation = repo.one(
                "SELECT * FROM delegations WHERE id=%s FOR UPDATE", (claim.row["delegation_id"],)
            )
            self.claims.assert_current(repo, claim)
            if repo.one(
                "SELECT id FROM delegation_results WHERE delegation_id=%s", (delegation["id"],)
            ):
                return
            child = repo.one(
                "SELECT * FROM sessions WHERE id=%s", (delegation["child_session_id"],)
            )
            turn = repo.one(
                "SELECT * FROM turns WHERE session_id=%s AND state='succeeded' ORDER B"
                "Y ordinal DESC LIMIT 1",
                (child["id"],),
            )
            if turn is None or not turn["evidence_complete"]:
                repo.execute(
                    "UPDATE delegations SET state='failed' WHERE id=%s", (delegation["id"],)
                )
                repo.event(
                    delegation["workspace_id"],
                    delegation["parent_session_id"],
                    "delegation.failed",
                    {"delegation_id": delegation["id"], "reason": "output_contract_invalid"},
                )
                return
            execution = repo.one(
                "SELECT * FROM executions WHERE turn_id=%s AND state='succeeded'", (turn["id"],)
            )
            require(execution is not None and execution["native_id"], "output_contract_invalid")
            outcome = turn["outcome"]
            try:
                value = validate_result(
                    outcome.get("result_text", outcome.get("text", "")), delegation["contract"]
                )
            except DomainError:
                repo.execute(
                    "UPDATE delegations SET state='failed' WHERE id=%s", (delegation["id"],)
                )
                repo.event(
                    delegation["workspace_id"],
                    delegation["parent_session_id"],
                    "delegation.failed",
                    {"delegation_id": delegation["id"], "reason": "output_contract_invalid"},
                )
                return
            if value["kind"] == "TestResult" and value["verdict"] == "pass":
                tools = repo.all(
                    "SELECT payload FROM session_events WHERE execution_id=%s AND type"
                    "='tool.completed'",
                    (execution["id"],),
                )
                observed = {
                    e["payload"].get("state", {}).get("input", {}).get("command"): e["payload"]
                    .get("state", {})
                    .get("metadata", {})
                    .get("exit")
                    for e in tools
                }
                valid_checks = bool(value["checks"]) and all(
                    c.get("command") in observed and observed[c["command"]] == 0
                    for c in value["checks"]
                )
                if not valid_checks:
                    repo.execute(
                        "UPDATE delegations SET state='failed' WHERE id=%s", (delegation["id"],)
                    )
                    repo.event(
                        delegation["workspace_id"],
                        delegation["parent_session_id"],
                        "delegation.failed",
                        {"delegation_id": delegation["id"], "reason": "output_contract_invalid"},
                    )
                    return
            result_id = new_id("res")
            repo.execute(
                "INSERT INTO delegation_results(id,workspace_id,delegation_id,child_se"
                "ssion_id,completing_turn_id,"
                "subject_digest,head_sha,kind,verdict,validated,value) VALUES(%s,%s,%s"
                ",%s,%s,%s,%s,%s,%s,true,%s)",
                (
                    result_id,
                    delegation["workspace_id"],
                    delegation["id"],
                    child["id"],
                    turn["id"],
                    value["subject_digest"],
                    value["head_sha"],
                    value["kind"],
                    value["verdict"],
                    value,
                ),
            )
            repo.execute(
                "UPDATE delegations SET state='succeeded' WHERE id=%s", (delegation["id"],)
            )
            repo.event(
                delegation["workspace_id"],
                delegation["parent_session_id"],
                "delegation.result_published",
                {
                    "delegation_id": delegation["id"],
                    "result_id": result_id,
                    "subject_digest": value["subject_digest"],
                },
            )
            waits = repo.all(
                "SELECT * FROM wait_subscriptions WHERE delegation_id=%s AND state='pe"
                "nding' FOR UPDATE",
                (delegation["id"],),
            )
            parent = repo.one(
                "SELECT * FROM sessions WHERE id=%s FOR UPDATE", (delegation["parent_session_id"],)
            )
            for wait in waits:
                repo.execute(
                    "UPDATE wait_subscriptions SET state=CASE WHEN expires_at>now() TH"
                    "EN 'satisfied' ELSE 'expired' END,"
                    "evidence_result_id=%s WHERE id=%s",
                    (result_id, wait["id"]),
                )
                if parent["lifecycle"] == "open":
                    principal = Principal(parent["creator_id"], (parent["workspace_id"],))
                    self.sessions.send_in(
                        repo,
                        parent,
                        principal,
                        {
                            "routing": "queue",
                            "content": "Child result "
                            + result_id
                            + " for subject "
                            + value["subject_digest"],
                        },
                    )
            # Wake notices are ordinary Messages, not hidden provider work.
