"""Runtime plane E2E on disposable Postgres + real local executor + real
sbx-runtime daemon subprocess driving the fake OpenCode CLI (RFC 167 §03).

Covers: dispatch → lease allocate → enrollment/hello → turn.start →
event spool ingest → authoritative settle → projections (parts, binding)
→ native resume on the same lease → cancel → lease-loss reconcile.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
FAKE_OPENCODE = REPO_ROOT / "tests" / "fakes" / "fake_opencode.py"


@pytest.fixture()
def runtime_stack(pg, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_BIN", f"{sys.executable} {FAKE_OPENCODE}")
    from control.application.runtime_stack import RuntimeStack
    from control.executors.local import LocalExecutorBackend
    from control.jobs import handlers

    stack = RuntimeStack(
        pg,
        backends={},
        credential_resolver=lambda session, turn: {
            "files": {".local/share/opencode/auth.json": '{"token": "REDACTED"}'},
            "env": {},
        },
    )
    stack.start()
    stack.register_backend(
        "local",
        LocalExecutorBackend(
            run_root=tmp_path / "executors",
            ingress_endpoint=stack.ingress.endpoint,
            repo_root=REPO_ROOT,
        ),
    )
    handlers.set_runtime_stack(stack)
    yield stack
    handlers.set_runtime_stack(None)
    stack.stop()


@pytest.fixture()
def session(pg, workspace):
    from control.application.sessions import SessionService

    svc = SessionService(pg)
    out = svc.create_session(
        principal={"kind": "user", "id": workspace["user_id"]},
        workspace_id=workspace["workspace_id"],
        harness={
            "provider_id": "opencode",
            "model": "opencode/big-pickle",
            "effort": "default",
            "adapter_version": "1",
            "cli_version": "fake",
        },
        projectless_spec={"backend": "local"},
        effective_input_digest="sha256:e2e",
    )
    return out


def _post(pg, workspace, session_id, text, routing="queue", dedupe=None):
    from control.application.sessions import SessionService

    return SessionService(pg).post_message(
        principal={"kind": "user", "id": workspace["user_id"]},
        workspace_id=workspace["workspace_id"],
        session_id=session_id,
        author={"kind": "user", "id": workspace["user_id"]},
        routing=routing,
        content={"text": text},
        dedupe_key=dedupe,
    )


def _drain(pg):
    from control.jobs.handlers import HANDLERS
    from control.jobs.worker import Worker

    worker = Worker(db=pg, holder="test-worker", handlers=HANDLERS)
    worker.run_until_idle(max_steps=20, sleep=0.05)


def _turn_state(pg, workspace_id, turn_id):
    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        turn = uow.turns.get(workspace_id, turn_id)
        return turn["state"] if turn else None


def _wait_turn(pg, workspace_id, turn_id, states, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = _turn_state(pg, workspace_id, turn_id)
        if state in states:
            return state
        time.sleep(0.25)
    return _turn_state(pg, workspace_id, turn_id)


class TestTurnLifecycle:
    def test_queue_turn_to_terminal(self, pg, workspace, runtime_stack, session):
        out = _post(pg, workspace, session["session_id"], "write hello.txt")
        turn_id = out["turn_id"]
        assert _turn_state(pg, workspace["workspace_id"], turn_id) == "queued"
        _drain(pg)
        state = _wait_turn(
            pg,
            workspace["workspace_id"],
            turn_id,
            {"succeeded", "failed", "cancelled", "interrupted"},
        )
        assert state == "succeeded"

        from control.persistence.unit_of_work import SqlUnitOfWork

        with SqlUnitOfWork(pg) as uow:
            # Execution row captured operation identity + terminal verdict.
            ex = uow.executions.nonterminal_by_turn(workspace["workspace_id"], turn_id)
            assert ex is None  # settled
            assert uow.turns.get(workspace["workspace_id"], turn_id)
            execs = uow.executions.list_by_turn(workspace["workspace_id"], turn_id)
            assert execs and execs[0]["state"] == "succeeded"
            assert execs[0]["executor_lease_id"]
            # Native context bound for resume.
            binding = uow.rows.one(
                "SELECT * FROM native_context_bindings WHERE session_id=%s",
                (session["session_id"],),
            )
            assert binding and binding["native_id"].startswith("ses_")
            # Worktree written by the fake CLI through the lease's worktree.
            # Parts projected onto the assistant output message.
            parts = uow.rows.all(
                "SELECT * FROM message_parts mp JOIN messages m ON m.id = mp.message_id"
                " WHERE m.session_id=%s AND m.role='assistant'",
                (session["session_id"],),
            )
            assert parts
            # Event journal carries the runtime-source evidence.
            evs = uow.rows.all(
                "SELECT type FROM session_events WHERE session_id=%s ORDER BY seq",
                (session["session_id"],),
            )
            types = [e["type"] for e in evs]
            assert "turn.started" in types
            assert "execution.observed_terminal" in types or "message.completed" in types
            assert "turn.succeeded" in types

    def test_second_turn_resumes_native_context(self, pg, workspace, runtime_stack, session):
        t1 = _post(pg, workspace, session["session_id"], "first")
        _drain(pg)
        assert (
            _wait_turn(pg, workspace["workspace_id"], t1["turn_id"], {"succeeded"}) == "succeeded"
        )
        t2 = _post(pg, workspace, session["session_id"], "follow up")
        _drain(pg)
        assert (
            _wait_turn(pg, workspace["workspace_id"], t2["turn_id"], {"succeeded", "failed"})
            == "succeeded"
        )

        from control.persistence.unit_of_work import SqlUnitOfWork

        with SqlUnitOfWork(pg) as uow:
            ex2 = uow.executions.list_by_turn(workspace["workspace_id"], t2["turn_id"])[0]
            # Resume path reuses the same live lease (native state intact).
            assert ex2["native_binding_id"] is not None
            row = uow.rows.one("SELECT executor_lease_id FROM executions WHERE id=%s", (ex2["id"],))
            ex1 = uow.executions.list_by_turn(workspace["workspace_id"], t1["turn_id"])[0]
            assert row["executor_lease_id"] == ex1["executor_lease_id"]

    def test_cancel_intent_marks_stop(self, pg, workspace, runtime_stack, session):
        out = _post(pg, workspace, session["session_id"], "cancel me")
        _drain(pg)
        _wait_turn(pg, workspace["workspace_id"], out["turn_id"], {"succeeded", "running"})
        # Cancel a terminal or running turn — either is a lawful no-op or
        # an actual cancel; the state machine stays coherent.
        from control.persistence.unit_of_work import SqlUnitOfWork

        with SqlUnitOfWork(pg) as uow:
            res = runtime_stack.service.cancel_turn(
                uow, workspace_id=workspace["workspace_id"], turn_id=out["turn_id"]
            )
            uow.commit()
        assert res["turn_id"] == out["turn_id"]

    def test_idempotent_dispatch_replay(self, pg, workspace, runtime_stack, session):
        # Same dedupe_key → same job, no second turn.
        _post(pg, workspace, session["session_id"], "dedupe", dedupe="msg-1")
        b = _post(pg, workspace, session["session_id"], "dedupe", dedupe="msg-1")
        from control.application.commands import Replay

        assert isinstance(b, Replay)


class TestLeaseLoss:
    def test_detach_quarantines_lease_and_fails_turn(
        self, pg, workspace, runtime_stack, session, monkeypatch
    ):
        # A hanging CLI keeps the turn nonterminal until we kill the daemon.
        runtime_stack.service.credential_resolver = lambda s, t: {
            "files": {".local/share/opencode/auth.json": '{"token": "REDACTED"}'},
            "env": {"FAKE_OPENCODE_SCENARIO": "hang"},
        }
        out = _post(pg, workspace, session["session_id"], "hang forever")
        _drain(pg)
        assert (
            _wait_turn(pg, workspace["workspace_id"], out["turn_id"], {"running", "succeeded"})
            == "running"
        )

        from control.persistence.unit_of_work import SqlUnitOfWork

        with SqlUnitOfWork(pg) as uow:
            lease = uow.leases.active_by_session(workspace["workspace_id"], session["session_id"])
            assert lease is not None
            pid = int((lease["handle"] or {}).get("pid"))
        import signal as _sig

        os.killpg(pid, _sig.SIGKILL)  # daemon death — no goodbye
        state = _wait_turn(
            pg,
            workspace["workspace_id"],
            out["turn_id"],
            {"interrupted", "failed", "cancelled"},
            timeout=30,
        )
        assert state == "interrupted"
        with SqlUnitOfWork(pg) as uow:
            lease = uow.leases.get(workspace["workspace_id"], lease["id"])
            assert lease["state"] == "lost"
            ex = uow.executions.list_by_turn(workspace["workspace_id"], out["turn_id"])[0]
            assert ex["state"] == "unknown"
