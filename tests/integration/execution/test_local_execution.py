"""End-to-end execution through the real runtime protocol (A04-A11, A14, A10)."""

from __future__ import annotations

import os
import signal

import pytest
from control.application.ingest import ingest
from control.application.ports import RuntimeUnavailable
from control.domain.errors import DomainError
from tests.support.factories import make_principal, session_body
from tests.support.stack import Stack, StaticBroker


@pytest.fixture
def stack(db, tmp_path):
    s = Stack(db, tmp_path)
    yield s
    s.shutdown()


def _start(stack, text, principal=None):
    principal = principal or make_principal(stack.db)
    created = stack.sessions.create(
        principal, principal.default_workspace_id, session_body(message={"content": text})
    )
    return principal, created["session_id"], created["turn_id"]


def test_turn_streams_parts_and_succeeds(stack) -> None:
    principal, sid, turn_id = _start(stack, "hello [write:a.txt=one]")
    stack.drive(stack.turn_done(turn_id))
    turn = stack.turn(turn_id)
    assert turn["state"] == "succeeded" and turn["evidence_complete"] is True
    assert (
        turn["usage"]["source"] == "opencode.step_finish" and turn["usage"]["input_tokens"] == 120
    )
    assert stack.output_text(turn_id).startswith("ACK: hello")
    types = [e["type"] for e in stack.events(sid)]
    for expected in (
        "turn.preparing",
        "executor.bound",
        "worktree.restored",
        "execution.preparing",
        "execution.started",
        "turn.started",
        "execution.native_bound",
        "tool.started",
        "tool.completed",
        "message.part_updated",
        "message.completed",
        "turn.succeeded",
    ):
        assert expected in types, expected
    assert types.index("turn.started") < types.index("turn.succeeded")
    runtime_events = [e for e in stack.events(sid) if e["source"] == "runtime"]
    assert [e["local_seq"] for e in runtime_events] == list(range(1, len(runtime_events) + 1))
    view = stack.queries.session(principal, sid)["session"]
    assert view["activity"] == "awaiting_input" and view["executor"]["lease_state"] == "ready"


def test_followup_uses_verified_native_resume_on_same_lease(stack) -> None:
    principal, sid, t1 = _start(stack, "remember the word ZEBRA")
    stack.drive(stack.turn_done(t1))
    t2 = stack.sessions.send(principal, sid, {"content": "which word? [recall]"})["turn_id"]
    stack.drive(stack.turn_done(t2))
    assert stack.turn(t2)["state"] == "succeeded"
    assert "remember the word ZEBRA" in stack.output_text(t2)
    bindings = stack.db.read(lambda u: u.find("native_context_bindings", {"session_id": sid}))
    assert len(bindings) == 1, "same native session id on resume"
    leases = stack.db.read(lambda u: u.find("executor_leases", {"session_id": sid}))
    assert len(leases) == 1 and leases[0]["state"] == "ready"


def test_cancel_running_turn_confirms_stop_and_next_turn_runs(stack) -> None:
    principal, sid, t1 = _start(stack, "block [hang]")
    stack.drive(lambda: stack.turn(t1)["state"] == "running")
    t2 = stack.sessions.send(principal, sid, {"content": "after"})["turn_id"]
    assert stack.sessions.cancel_turn(principal, t1)["turn"]["state"] == "cancelling"
    stack.drive(stack.turn_done(t1))
    assert stack.turn(t1)["state"] == "cancelled"
    execution = stack.db.read(lambda u: u.find_one("executions", {"turn_id": t1}))
    assert execution["state"] == "cancelled"
    stack.drive(stack.turn_done(t2))
    assert stack.turn(t2)["state"] == "succeeded"


def test_runtime_loss_interrupts_requires_ack_and_refuses_missing_native_state(stack) -> None:
    """A14: kill executor mid-Turn -> interrupted/unknown, history kept, no blind resume."""
    principal, sid, t1 = _start(stack, "long [hang]")
    stack.drive(
        lambda: (
            stack.db.read(lambda u: u.count("native_context_bindings", {"session_id": sid})) == 1
        )
    )
    lease = stack.db.read(lambda u: u.find_one("executor_leases", {"session_id": sid}))
    os.killpg(lease["handle"]["pid"], signal.SIGKILL)
    stack.drive(stack.turn_done(t1), timeout=40)
    turn = stack.turn(t1)
    assert turn["state"] == "interrupted" and turn["reason"] == "outcome_unknown"
    assert turn["evidence_complete"] is False
    lease = stack.db.read(lambda u: u.get("executor_leases", lease["id"]))
    assert lease["state"] == "lost" and lease["quarantined"] is False
    t2 = stack.sessions.send(principal, sid, {"content": "continue [recall]"})["turn_id"]
    assert stack.db.read(lambda u: u.count("jobs", {"kind": "turn.dispatch", "turn_id": t2})) == 0
    stack.sessions.acknowledge_unknown(principal, t1)
    stack.drive(stack.turn_done(t2), timeout=40)
    second = stack.turn(t2)
    assert second["state"] == "failed" and second["reason"] == "context_unavailable"
    leases = stack.db.read(
        lambda u: u.find("executor_leases", {"session_id": sid}, order="generation")
    )
    assert [lz["generation"] for lz in leases] == [1, 2]


def test_release_checkpoint_then_restore_preserves_files_and_native_lineage(stack) -> None:
    """A11/A16: checkpoint on release, new lease restores files + native state."""
    principal, sid, t1 = _start(stack, "remember GAMMA [write:g.txt=gamma]")
    stack.drive(stack.turn_done(t1))
    stack.execution.release(principal, sid)
    stack.drive(
        lambda: (
            stack.db.read(lambda u: u.find_one("worktrees", {"session_id": sid}))["availability"]
            == "checkpointed"
        )
    )
    snapshot = stack.db.read(lambda u: u.find_one("snapshots", {"kind": "checkpoint"}))
    assert snapshot["state"] == "ready"
    assert stack.queries.session(principal, sid)["session"]["executor"]["lease_id"] is None
    t2 = stack.sessions.send(principal, sid, {"content": "recall please [recall]"})["turn_id"]
    stack.drive(stack.turn_done(t2), timeout=40)
    assert stack.turn(t2)["state"] == "succeeded"
    assert "remember GAMMA" in stack.output_text(t2)
    lease = stack.db.read(
        lambda u: u.find_one("executor_leases", {"session_id": sid, "state": "ready"})
    )
    assert lease["generation"] == 2
    content = stack.connector.channel(lease).query("files.read", path="g.txt")["content"]
    assert content == "gamma\n"
    restored = [e for e in stack.events(sid) if e["type"] == "worktree.restored"]
    assert restored[-1]["payload"]["restored_from"] == "checkpoint"


class _Flaky:
    def __init__(self, real, mode):
        self.real, self.mode, self.fired = real, mode, False

    def __getattr__(self, name):
        return getattr(self.real, name)

    def op(self, kind, *args, **kwargs):
        if kind == "turn.start" and not self.fired:
            self.fired = True
            if self.mode == "after":
                self.real.op(kind, *args, **kwargs)
            raise RuntimeUnavailable("dropped")
        return self.real.op(kind, *args, **kwargs)


@pytest.mark.parametrize("mode", ["after", "before"])
def test_lost_start_response_never_double_launches(stack, mode) -> None:
    """A06: response lost after acceptance -> adopt; never reached runtime -> safe resend."""
    real_channel = stack.connector.channel
    flaky: dict[str, _Flaky] = {}

    def channel(lease):
        flaky.setdefault(lease["id"], _Flaky(real_channel(lease), mode))
        flaky[lease["id"]].real = real_channel(lease)
        return flaky[lease["id"]]

    stack.connector.channel = channel
    principal, sid, t1 = _start(stack, "once")
    stack.drive(stack.turn_done(t1))
    assert stack.turn(t1)["state"] == "succeeded"
    assert [e["type"] for e in stack.events(sid)].count("execution.started") == 1
    assert stack.db.read(lambda u: u.count("executions", {"turn_id": t1})) == 1


def test_evidence_replay_is_deduped_and_conflicts_detected(stack) -> None:
    """A08: duplicate batches produce no new journal facts; changed payload is integrity error."""
    principal, sid, t1 = _start(stack, "x")
    stack.drive(stack.turn_done(t1))
    lease = stack.db.read(lambda u: u.find_one("executor_leases", {"session_id": sid}))
    batch = stack.connector.channel(lease).events(0)
    before = len(stack.events(sid))
    result = stack.db.run(
        lambda u: ingest(u, lease, batch["runtime_epoch"], batch["items"], stack.execution.hooks)
    )
    assert result.applied == 0 and len(stack.events(sid)) == before
    tampered = [dict(batch["items"][0], payload={"provider_id": "evil"})]
    with pytest.raises(DomainError):
        stack.db.run(
            lambda u: ingest(u, lease, batch["runtime_epoch"], tampered, stack.execution.hooks)
        )


def test_invalid_credential_fails_and_reports_health(db, tmp_path) -> None:
    broker = StaticBroker(api_key="invalid-zen-key-0000")
    stack = Stack(db, tmp_path, broker=broker)
    try:
        _, sid, t1 = _start(stack, "x")
        stack.drive(stack.turn_done(t1))
        turn = stack.turn(t1)
        assert turn["state"] == "failed" and turn["reason"] == "credential_invalid"
        assert "invalid" in broker.health
        assert "invalid-zen-key-0000" not in str(stack.events(sid))
    finally:
        stack.shutdown()


def test_reads_never_wake_compute(stack) -> None:
    """A10: projections/executor views on released compute never allocate."""
    principal, sid, t1 = _start(stack, "x")
    stack.drive(stack.turn_done(t1))
    stack.execution.release(principal, sid)
    stack.drive(
        lambda: (
            stack.db.read(lambda u: u.find_one("executor_leases", {"session_id": sid}))["state"]
            == "released"
        )
    )
    for _ in range(3):
        stack.queries.session(principal, sid)
        stack.execution.executor_view(principal, sid)
        stack.queries.events(principal, sid)
    assert stack.db.read(lambda u: u.count("executor_leases", {"session_id": sid})) == 1
    assert stack.db.read(lambda u: u.count("jobs", {"state": ["queued", "retry_wait"]})) == 0
