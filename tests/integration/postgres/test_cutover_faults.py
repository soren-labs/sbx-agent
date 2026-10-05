import os

import pytest
from control.application.lifecycle import Lifecycle
from control.application.sessions import Sessions
from control.application.worktrees import Worktrees
from control.domain.errors import DomainError
from control.jobs.claims import Claims
from control.jobs.handlers.delegation import DeadlineHandler
from control.jobs.handlers.outbox import OutboxNotify
from control.jobs.handlers.snapshots import ReleaseHandler
from control.persistence.database import Database
from tests.integration.postgres.test_delivery_delegation import setup
from tests.integration.postgres.test_execution import prepare


def test_outbox_reclaim_notifies_only_committed_watermark(database, principal):
    sessions = Sessions(database)
    sid = sessions.create(principal, principal.workspace_ids[0], {}, "create")["session_id"]
    claims = Claims(database, table="outbox_messages")
    old = claims.take("old")
    with database.transaction() as repo:
        repo.execute("UPDATE outbox_messages SET claim_expires_at=now()-interval '1 second'")
    reclaimed = claims.take("new")
    assert reclaimed.row["event_id"] == old.row["event_id"]
    with pytest.raises(DomainError, match="version_conflict"):
        OutboxNotify(database, claims)(old)
    OutboxNotify(Database(database.dsn), claims)(reclaimed)
    claims.finish(reclaimed)
    with database.transaction() as repo:
        assert repo.one("SELECT state FROM outbox_messages")["state"] == "succeeded"
        assert (
            repo.one("SELECT count(*) AS n FROM session_events WHERE session_id=%s", (sid,))["n"]
            == 1
        )


def test_release_rejects_active_compute_and_keeps_ambiguous_teardown(database, principal):
    sessions, claim, execution, lease, ingest = prepare(database, principal)
    with pytest.raises(DomainError, match="waiting_capacity"):
        Worktrees(database).release(principal, lease["session_id"], "release")
    from control.application.execution import Execution

    claims = Claims(database)
    Execution(database, claims).lost(claim, execution["id"])
    claims.finish(claim)
    receipt = Worktrees(database).release(principal, lease["session_id"], "release")
    release = claims.take("cleanup")
    assert release.job_id == receipt["job_id"]

    class Ambiguous:
        def lookup(self, operation):
            return None

    with database.transaction() as repo:
        repo.execute("UPDATE executor_leases SET handle=NULL")
    with pytest.raises(DomainError, match="outcome_unknown"):
        ReleaseHandler(database, claims, lambda *args: Ambiguous(), os.urandom(32))(release)
    with database.transaction() as repo:
        assert not repo.one("SELECT cleanup_confirmed FROM executor_leases")["cleanup_confirmed"]


def test_wait_timeout_is_durable_and_subtree_cancel_is_idempotent(database, principal):
    from control.application.delegation import Delegations

    _, sessions, sid, cs = setup(database, principal)
    service = Delegations(database, sessions)
    child = service.spawn(principal, sid, {"changeset_id": cs}, "child")
    wait = service.wait(principal, child["delegation_id"], "wait", seconds=1)
    with database.transaction() as repo:
        repo.execute("UPDATE jobs SET state='succeeded' WHERE kind='turn.dispatch'")
        repo.execute("UPDATE jobs SET due_at=now() WHERE kind='delegation.expire_wait'")
    claims = Claims(database)
    claim = claims.take("timeout")
    assert claim.row["kind"] == "delegation.expire_wait"
    DeadlineHandler(database, claims, service)(claim)
    claims.finish(claim)
    with database.transaction() as repo:
        assert (
            repo.one(
                "SELECT state FROM wait_subscriptions WHERE id=%s", (wait["subscription_id"],)
            )["state"]
            == "expired"
        )
    first = service.cancel(principal, child["delegation_id"], "cancel")
    assert service.cancel(principal, child["delegation_id"], "cancel") == first
    with database.transaction() as repo:
        assert (
            repo.one("SELECT count(*) AS n FROM session_events WHERE type='delegation.cancelled'")[
                "n"
            ]
            == 1
        )


def test_linked_continuation_has_fresh_native_worktree(database, principal):
    sessions = Sessions(database)
    sid = sessions.create(principal, principal.workspace_ids[0], {}, "create")["session_id"]
    result = Lifecycle(database, sessions).continuation(
        principal, sid, {"summary": "explicit user summary"}, "continue"
    )
    assert result["native_context"] == "fresh"
    assert result["session_id"] != sid
    with database.transaction() as repo:
        assert (
            repo.one(
                "SELECT linked_from_session_id FROM sessions WHERE id=%s", (result["session_id"],)
            )["linked_from_session_id"]
            == sid
        )
        assert repo.one("SELECT count(*) AS n FROM native_context_bindings")["n"] == 0
        assert repo.one("SELECT count(*) AS n FROM worktrees")["n"] == 2


def test_terminal_open_defaults_and_failure_evidence(database, principal):
    from control.application.io import SessionIO
    from control.jobs.handlers.io import IOHandler

    sessions = Sessions(database)
    wid = principal.workspace_ids[0]
    sid = sessions.create(principal, wid, {}, "session")["session_id"]
    with database.transaction() as repo:
        repo.execute(
            "INSERT INTO executor_leases(id,workspace_id,session_id,backend,state,gene"
            "ration,allocation_operation_id,expires_at) VALUES('lease',%s,%s,'local','"
            "ready',1,'allocate',now()+interval '1 hour')",
            (wid, sid),
        )
    claims = Claims(database)
    calls = []

    class Client:
        hello = {"lease_id": "lease", "lease_generation": 1}

        def submit(self, oid, kind, payload):
            calls.append(payload)

        def wait(self, oid):
            return {"process_id": oid, "running": True}

    class Backend:
        def connect_runtime(self, handle):
            return Client()

    io = SessionIO(database, claims, lambda *args: Backend(), os.urandom(32), None)
    accepted = io.operation(principal, sid, "terminal.open", {}, "open")
    claim = claims.take("terminal")
    IOHandler(database, claims, io, None)(claim)
    assert calls[0]["terminal_id"] == accepted["operation_id"]
    assert calls[0]["command"] == ["/bin/bash", "--noprofile", "--norc"]


def test_capture_rejects_running_service_before_barrier(database, principal):
    from control.application.changes import Changes

    sessions, claim, execution, lease, ingest = prepare(database, principal)
    event = {
        "local_seq": 1,
        "operation_id": execution["operation_id"],
        "type": "execution.stopped",
        "payload": {"stopped": True},
    }
    ingest.batch(claim, execution["id"], "epoch", [event])
    ingest.finish(
        claim,
        execution["id"],
        "epoch",
        {"outcome": "success", "stopped": True, "final_watermark": 1},
    )
    with database.transaction() as repo:
        repo.execute(
            "INSERT INTO service_instances(id,workspace_id,session_id,name,lease_id,le"
            "ase_generation,state) VALUES('service',%s,%s,'web',%s,1,'running')",
            (principal.workspace_ids[0], lease["session_id"], lease["id"]),
        )
    with pytest.raises(DomainError, match="waiting_capacity"):
        Changes(database, None).capture(
            principal, lease["session_id"], {"generation": 1}, "capture"
        )
    with database.transaction() as repo:
        assert repo.one("SELECT count(*) AS n FROM worktree_operations")["n"] == 0
