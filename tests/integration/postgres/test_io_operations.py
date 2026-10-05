import os

import pytest
from control.application.execution import Execution
from control.application.io import SessionIO
from control.application.sessions import Sessions
from control.domain.errors import DomainError
from control.jobs.claims import Claims
from control.jobs.handlers.io import IOHandler


def test_file_effect_response_loss_reclaim_and_dispatch_barrier(database, principal):
    sessions = Sessions(database)
    wid = principal.workspace_ids[0]
    sid = sessions.create(principal, wid, {"backend": "local"}, "session")["session_id"]
    with database.transaction() as repo:
        repo.execute(
            "INSERT INTO executor_leases(id,workspace_id,session_id,backend,state,gene"
            "ration,allocation_operation_id,expires_at) "
            "VALUES('lease_test',%s,%s,'local','ready',1,'allocation_test',now()+inter"
            "val '1 hour')",
            (wid, sid),
        )
    claims = Claims(database)

    class Client:
        hello = {"lease_id": "lease_test", "lease_generation": 1}
        effects = {}
        lose = True

        def submit(self, operation, kind, payload):
            self.effects.setdefault(operation, {"generation": payload["generation"] + 1})

        def wait(self, operation):
            if self.lose:
                self.lose = False
                raise RuntimeError("response lost after file saved")
            return self.effects[operation]

    client = Client()

    class Backend:
        def connect_runtime(self, handle):
            return client

    io = SessionIO(database, claims, lambda *args: Backend(), os.urandom(32), None)
    op = io.operation(
        principal,
        sid,
        "files.write",
        {"path": "README.md", "content": "work", "generation": 0},
        "write",
    )
    claimed = claims.take("writer")
    turn = sessions.send(principal, sid, {"content": "queued during mutation"}, "queued")
    waiting = claims.take("coder")
    with pytest.raises(DomainError, match="waiting_capacity"):
        Execution(database, claims).admit(waiting)
    claims.finish(waiting, "retry_wait", delay=30)
    handler = IOHandler(database, claims, io, None)
    with pytest.raises(RuntimeError):
        handler(claimed)
    with database.transaction() as repo:
        repo.execute(
            "UPDATE jobs SET claim_expires_at=now()-interval '1 second' WHERE id=%s",
            (claimed.job_id,),
        )
    successor = claims.take("successor")
    with pytest.raises(DomainError, match="version_conflict"):
        handler(claimed)
    handler(successor)
    claims.finish(successor)
    assert len(client.effects) == 1
    with database.transaction() as repo:
        assert repo.one("SELECT generation FROM worktrees")["generation"] == 1
        assert (
            repo.one("SELECT state FROM worktree_operations WHERE id=%s", (op["operation_id"],))[
                "state"
            ]
            == "succeeded"
        )
        assert (
            repo.one("SELECT state FROM turns WHERE id=%s", (turn["turn_id"],))["state"] == "queued"
        )
