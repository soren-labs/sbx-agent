import pytest
from control.application.execution import Execution
from control.application.ingest import Ingest
from control.application.sessions import Sessions
from control.domain.errors import DomainError
from control.jobs.claims import Claims


def prepare(database, principal):
    sessions = Sessions(database)
    sid = sessions.create(principal, principal.workspace_ids[0], {"backend": "local"}, "create")[
        "session_id"
    ]
    sessions.send(principal, sid, {"content": "work"}, "send")
    claims = Claims(database)
    claim = claims.take("worker")
    app = Execution(database, claims)
    session, turn, execution, lease = app.admit(claim)
    app.bind(
        claim,
        session,
        lease,
        "fake",
        {"lease_id": lease["id"], "lease_generation": lease["generation"]},
    )
    app.started(claim, session, turn, execution)
    return sessions, claim, execution, lease, Ingest(database, claims)


@pytest.mark.parametrize(
    "cancel_first,complete,expected",
    [(False, True, "succeeded"), (True, True, "cancelled"), (False, False, "interrupted")],
)
def test_terminal_evidence_cancel_and_watermark(
    database, principal, cancel_first, complete, expected
):
    sessions, claim, execution, lease, ingest = prepare(database, principal)
    if cancel_first:
        sessions.cancel(principal, execution["turn_id"], "cancel")
    event = {
        "local_seq": 1,
        "operation_id": execution["operation_id"],
        "type": "execution.stopped",
        "payload": {"stopped": True},
    }
    assert ingest.batch(claim, execution["id"], "epoch", [event]) == 1
    assert ingest.batch(claim, execution["id"], "epoch", [event]) == 1
    result = {"outcome": "success", "stopped": True, "final_watermark": 1 if complete else 2}
    assert ingest.finish(claim, execution["id"], "epoch", result) == expected
    assert ingest.finish(claim, execution["id"], "epoch", result) == expected
    with database.transaction() as repo:
        assert (
            repo.one("SELECT count(*) AS n FROM session_events WHERE local_seq IS NOT NULL")["n"]
            == 1
        )


def test_source_gap_conflict_and_unknown_blocks_dispatch(database, principal):
    sessions, claim, execution, lease, ingest = prepare(database, principal)
    event = {
        "local_seq": 2,
        "operation_id": execution["operation_id"],
        "type": "execution.stopped",
        "payload": {},
    }
    with pytest.raises(DomainError, match="invalid_cursor"):
        ingest.batch(claim, execution["id"], "epoch", [event])
    event["local_seq"] = 1
    ingest.batch(claim, execution["id"], "epoch", [event])
    event["payload"] = {"changed": True}
    with pytest.raises(DomainError, match="idempotency_conflict"):
        ingest.batch(claim, execution["id"], "epoch", [event])
    ingest.finish(claim, execution["id"], "epoch", {"outcome": "unknown", "stopped": False})
    sid = sessions.get(principal, execution and lease["session_id"])["id"]
    sessions.send(principal, sid, {"content": "follow-up"}, "follow")
    claims = Claims(database)
    claims.finish(claim)
    with pytest.raises(DomainError, match="outcome_unknown"):
        Execution(database, claims).admit(claims.take("next"))


def test_runtime_loss_keeps_identity_history_and_quarantines(database, principal):
    sessions, claim, execution, lease, ingest = prepare(database, principal)
    app = Execution(database, Claims(database))
    app.lost(claim, execution["id"])
    snapshot = sessions.get(principal, lease["session_id"])
    assert snapshot["turns"][0]["state"] == "interrupted"
    with database.transaction() as repo:
        wt = repo.one("SELECT * FROM worktrees WHERE session_id=%s", (snapshot["id"],))
        assert wt["availability"] == "unavailable"
        assert repo.one("SELECT state,cleanup_confirmed FROM executor_leases")["state"] == "lost"
        assert not repo.one("SELECT cleanup_confirmed FROM executor_leases")["cleanup_confirmed"]
        assert repo.one("SELECT state FROM executions")["state"] == "unknown"
    assert snapshot["messages"][0]["content"] == "work"
