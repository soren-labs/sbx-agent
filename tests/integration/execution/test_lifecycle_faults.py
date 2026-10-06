"""Lease allocation/release fault handling and the unknown-outcome gate (RFC 02/03, A14).

A controllable fake backend injects lookup failures, lost responses, unconfirmed
termination and dead allocations; Local executor tests use real subprocesses.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import timedelta
from typing import Any

import pytest
from control.domain.errors import DomainError
from control.domain.ids import new_id
from control.executors.local import LocalExecutor, _alive
from control.jobs.timers import enqueue_quarantine_releases
from tests.support.factories import make_principal, session_body
from tests.support.runtime import FAKE_OPENCODE
from tests.support.stack import Stack


class FakeBackend:
    """Executor port double keyed by allocation operation id."""

    kind = "local"

    def __init__(self) -> None:
        self.allocations: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.lookup_fails = False
        self.terminate_confirms = True
        self.on_allocate: Any = None

    def capabilities(self) -> dict[str, Any]:
        return {"backend": "local"}

    def seed(self, op: str, *, alive: bool = True) -> dict[str, Any]:
        self.allocations[op] = {"sandbox_id": f"sb-{op}", "operation_id": op, "alive": alive}
        return {"sandbox_id": f"sb-{op}", "operation_id": op}

    def _view(self, op: str) -> dict[str, Any]:
        a = self.allocations[op]
        status = "running" if a["alive"] else "terminated"
        return {"sandbox_id": a["sandbox_id"], "operation_id": op, "status": status}

    def lookup(self, operation_id: str, compute: Any) -> dict[str, Any] | None:
        self.calls.append(("lookup", operation_id))
        if self.lookup_fails:
            raise DomainError("executor_unavailable", "lookup failed", retryable=True)
        return self._view(operation_id) if operation_id in self.allocations else None

    def allocate(self, spec: dict[str, Any], operation_id: str) -> dict[str, Any]:
        self.calls.append(("allocate", operation_id))
        if operation_id not in self.allocations:
            self.seed(operation_id)
        if self.on_allocate:
            self.on_allocate(spec)
        return self._view(operation_id)

    def describe(self, handle: dict[str, Any], compute: Any) -> dict[str, Any]:
        self.calls.append(("describe", handle["operation_id"]))
        return {"status": self._view(handle["operation_id"])["status"]}

    def connect_runtime(self, handle: dict[str, Any], compute: Any) -> str:
        raise DomainError("executor_unavailable", "runtime not reachable", retryable=True)

    def terminate(self, handle: dict[str, Any], operation_id: str, compute: Any) -> bool:
        self.calls.append(("terminate", handle["operation_id"]))
        if not self.terminate_confirms:
            return False
        self.allocations[handle["operation_id"]]["alive"] = False
        return True

    def count(self, kind: str) -> int:
        return sum(1 for c in self.calls if c[0] == kind)


@pytest.fixture
def stack(db, tmp_path):
    s = Stack(db, tmp_path)
    yield s
    s.shutdown()


@pytest.fixture
def fake(stack):
    backend = FakeBackend()
    stack.execution.executors["local"] = backend
    stack.shutdown = lambda: None  # nothing real to clean up
    return backend


def _session(stack) -> tuple[Any, str]:
    principal = make_principal(stack.db)
    sid = stack.sessions.create(principal, principal.default_workspace_id, session_body())[
        "session_id"
    ]
    return principal, sid


def _lease(stack, sid: str) -> dict[str, Any]:
    return stack.db.read(
        lambda u: u.find_one("executor_leases", {"session_id": sid}, order="generation DESC")
    )


def _run_due(stack, rounds: int = 20) -> None:
    """Run every queued/retrying Job now (bounded: Continue loops would never idle)."""
    for _ in range(rounds):
        stack.db.run(
            lambda u: u.update_where(
                "jobs", {"state": ["queued", "retry_wait"]}, {"due_at": u.now()}
            )
        )
        if not stack.worker.run_once():
            return


def _jobs(stack, kind: str) -> list[dict[str, Any]]:
    return stack.db.read(lambda u: u.find("jobs", {"kind": kind}, order="created_at"))


def _set_lease(stack, lease_id: str, values: dict[str, Any]) -> None:
    stack.db.run(lambda u: u.update("executor_leases", lease_id, values))


# ------------------------------------------------------------------ allocation (item 3)
def test_handle_is_persisted_before_runtime_handshake(stack, fake) -> None:
    principal, sid = _session(stack)
    stack.execution.activate(principal, sid)
    _run_due(stack, rounds=1)
    lease = _lease(stack, sid)
    op = lease["allocation_operation_id"]
    assert lease["state"] == "allocating", "handshake failed"
    assert lease["handle"] == {"sandbox_id": f"sb-{op}", "operation_id": op}
    assert lease["observed_status"] == "allocated"
    _run_due(stack, rounds=3)
    assert fake.count("allocate") == 1, "retries observe the persisted handle, never re-create"
    assert fake.count("describe") >= 1


def test_terminated_allocation_is_confirmed_not_reallocated(stack, fake) -> None:
    principal, sid = _session(stack)
    stack.execution.activate(principal, sid)
    _run_due(stack, rounds=1)
    lease = _lease(stack, sid)
    fake.allocations[lease["allocation_operation_id"]]["alive"] = False
    _run_due(stack, rounds=3)
    lease = _lease(stack, sid)
    assert lease["state"] == "lost" and lease["state_reason"] == "allocation_terminated"
    assert lease["quarantined"] is False, "a dead allocation is confirmed isolation"
    assert fake.count("allocate") == 1


def test_lookup_failure_in_allocation_never_creates_blind(stack, fake) -> None:
    principal, sid = _session(stack)
    fake.lookup_fails = True
    stack.execution.activate(principal, sid)
    _run_due(stack, rounds=3)
    assert fake.count("lookup") >= 1 and fake.count("allocate") == 0
    assert _lease(stack, sid)["state"] == "allocating"


def test_lost_allocate_response_is_adopted_by_operation(stack, fake) -> None:
    principal, sid = _session(stack)
    stack.execution.activate(principal, sid)
    lease = _lease(stack, sid)
    fake.seed(lease["allocation_operation_id"])  # created, but the response was lost
    _run_due(stack, rounds=1)
    assert fake.count("allocate") == 0, "adopted via lookup"
    assert _lease(stack, sid)["handle"]["operation_id"] == lease["allocation_operation_id"]


def test_release_fencing_during_allocate_terminates_the_new_allocation(stack, fake) -> None:
    principal, sid = _session(stack)
    stack.execution.activate(principal, sid)
    lease = _lease(stack, sid)
    fake.on_allocate = lambda spec: stack.db.run(
        lambda u: stack.execution._quiesce(u, spec["lease_id"])
    )
    _run_due(stack, rounds=1)
    assert ("terminate", lease["allocation_operation_id"]) in fake.calls
    row = _lease(stack, sid)
    assert row["state"] == "quiescing" and row["handle"] is not None, "release can still find it"


# --------------------------------------------------------------------- release (item 4)
def test_release_with_missing_handle_terminates_allocation_found_by_operation(stack, fake) -> None:
    principal, sid = _session(stack)
    stack.execution.activate(principal, sid)
    lease = _lease(stack, sid)
    fake.seed(lease["allocation_operation_id"])
    stack.db.run(
        lambda u: u.update_where("jobs", {"kind": "executor.allocate"}, {"state": "cancelled"})
    )
    assert lease["handle"] is None
    stack.execution.release(principal, sid)
    _run_due(stack)
    assert ("terminate", lease["allocation_operation_id"]) in fake.calls
    row = _lease(stack, sid)
    assert row["state"] == "released" and row["quarantined"] is False
    assert fake.allocations[lease["allocation_operation_id"]]["alive"] is False


def test_release_never_confirms_when_allocation_cannot_be_resolved(stack, fake) -> None:
    principal, sid = _session(stack)
    stack.execution.activate(principal, sid)
    lease = _lease(stack, sid)
    stack.db.run(
        lambda u: u.update_where("jobs", {"kind": "executor.allocate"}, {"state": "cancelled"})
    )
    fake.lookup_fails = True
    stack.execution.release(principal, sid)
    _run_due(stack, rounds=3)
    row = _lease(stack, sid)
    assert row["state"] != "released" and row["quarantined"] is True
    assert fake.count("terminate") == 0
    # Exhaust the release Job: the lease stays quarantined, never "released".
    stack.db.run(
        lambda u: u.update_where(
            "jobs", {"kind": "executor.release"}, {"state": "failed", "finished_at": u.now()}
        )
    )
    with pytest.raises(DomainError) as err:
        stack.execution._new_lease(*stack.db.read(lambda u: (u, u.get("sessions", sid))))
    assert err.value.code == "outcome_unknown"
    # The quarantine sweep keeps resolving it once the backend answers again.
    assert enqueue_quarantine_releases(stack.db) == 1
    fake.lookup_fails = False
    _run_due(stack)
    row = _lease(stack, sid)
    assert row["state"] == "released" and row["quarantined"] is False
    assert row["id"] == lease["id"]


def test_release_requires_confirmed_stop(stack, fake) -> None:
    principal, sid = _session(stack)
    stack.execution.activate(principal, sid)
    _run_due(stack, rounds=1)
    lease = _lease(stack, sid)
    assert lease["handle"] is not None
    stack.db.run(
        lambda u: u.update_where("jobs", {"kind": "executor.allocate"}, {"state": "cancelled"})
    )
    fake.terminate_confirms = False
    stack.execution.release(principal, sid)
    _run_due(stack, rounds=3)
    row = _lease(stack, sid)
    assert row["state"] == "quiescing" and row["quarantined"] is True
    fake.terminate_confirms = True
    _run_due(stack)
    row = _lease(stack, sid)
    assert row["state"] == "released" and row["quarantined"] is False


def test_release_of_never_allocated_lease_is_confirmed_by_authoritative_lookup(stack, fake) -> None:
    principal, sid = _session(stack)
    stack.execution.activate(principal, sid)
    stack.db.run(
        lambda u: u.update_where("jobs", {"kind": "executor.allocate"}, {"state": "cancelled"})
    )
    stack.execution.release(principal, sid)
    _run_due(stack)
    row = _lease(stack, sid)
    assert fake.count("lookup") == 1 and fake.count("terminate") == 0
    assert row["state"] == "released"


def test_release_waits_while_a_requested_allocate_may_be_in_flight(stack, fake) -> None:
    principal, sid = _session(stack)
    stack.execution.activate(principal, sid)
    lease = _lease(stack, sid)
    stack.db.run(
        lambda u: u.update_where("jobs", {"kind": "executor.allocate"}, {"state": "cancelled"})
    )
    _set_lease(stack, lease["id"], {"observed_status": "allocate_requested"})
    stack.db.run(lambda u: u.update("executor_leases", lease["id"], {"observed_at": u.now()}))
    stack.execution.release(principal, sid)
    _run_due(stack, rounds=3)
    row = _lease(stack, sid)
    assert row["state"] == "quiescing", "absence is not yet authoritative"
    stack.db.run(
        lambda u: u.update(
            "executor_leases", lease["id"], {"observed_at": u.now() - timedelta(hours=1)}
        )
    )
    _run_due(stack)
    row = _lease(stack, sid)
    assert row["state"] == "quiescing" and row["quarantined"] is True
    fake.seed(lease["allocation_operation_id"])
    _run_due(stack)
    assert _lease(stack, sid)["state"] == "released"
    assert not fake.allocations[lease["allocation_operation_id"]]["alive"]


def test_release_quarantines_create_pending_beyond_allocation_window(stack, fake, monkeypatch):
    principal, sid = _session(stack)
    stack.execution.activate(principal, sid)
    lease = _lease(stack, sid)
    requested, finish = threading.Event(), threading.Event()
    errors = []

    def delayed_allocate(spec, operation_id):
        requested.set()
        assert finish.wait(15)
        return (
            fake._view(operation_id)
            if operation_id in fake.allocations
            else fake.seed(operation_id)
        )

    monkeypatch.setattr(fake, "allocate", delayed_allocate)

    def allocate():
        try:
            stack.worker.run_once()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=allocate)
    worker.start()
    try:
        assert requested.wait(10)
        assert _lease(stack, sid)["observed_status"] == "allocate_requested"
        _set_lease(stack, lease["id"], {"observed_at": stack.execution._now() - timedelta(hours=1)})
        stack.execution.release(principal, sid)
        _run_due(stack, rounds=3)
        row = _lease(stack, sid)
        assert row["state"] == "quiescing" and row["quarantined"] is True
        with pytest.raises(DomainError, match="isolation"):
            stack.db.run(lambda u: stack.execution._new_lease(u, u.get("sessions", sid)))
        finish.set()
        worker.join(timeout=10)
        assert not worker.is_alive() and not errors
        _run_due(stack)
        row = _lease(stack, sid)
        assert row["state"] == "released" and row["quarantined"] is False
        assert not fake.allocations[lease["allocation_operation_id"]]["alive"]
        assert ("terminate", lease["allocation_operation_id"]) in fake.calls
    finally:
        finish.set()
        worker.join(timeout=10)
        if lease["allocation_operation_id"] in fake.allocations:
            fake.allocations[lease["allocation_operation_id"]]["alive"] = False


# -------------------------------------------------------------- Local executor (item 6)
def _spec(lease_id: str = "lease_local1") -> dict[str, Any]:
    return {"lease_id": lease_id, "generation": 1, "enrollment_key": "00" * 32}


def test_local_failed_start_is_killed_and_retry_does_not_respawn(tmp_path, monkeypatch) -> None:
    executor = LocalExecutor(tmp_path / "ex", start_timeout=0.5)
    spawned: list[subprocess.Popen] = []

    def never_ready(spec, lease_dir, port_file):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
        )
        spawned.append(proc)
        return proc

    monkeypatch.setattr(executor, "_spawn", never_ready)
    with pytest.raises(DomainError) as err:
        executor.allocate(_spec(), "op_local1")
    assert err.value.retryable
    spawned[0].wait(timeout=5)
    assert not _alive(spawned[0].pid), "the failed start was killed, not leaked"
    assert executor.lookup("op_local1", None) is None, "forgotten only after confirmed death"
    with pytest.raises(DomainError):
        executor.allocate(_spec(), "op_local1")
    assert len(spawned) == 2 and all(p.wait(timeout=5) is not None for p in spawned)
    assert not any(_alive(p.pid) for p in spawned), "never two daemons, none leaked"


def test_local_unkillable_failed_start_stays_registered_and_is_adopted(
    tmp_path, monkeypatch
) -> None:
    executor = LocalExecutor(tmp_path / "ex", start_timeout=0.3)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    monkeypatch.setattr(executor, "_spawn", lambda *a: proc)
    monkeypatch.setattr(executor, "terminate", lambda *a: False)
    try:
        with pytest.raises(DomainError):
            executor.allocate(_spec(), "op_local2")
        found = executor.lookup("op_local2", None)
        assert found is not None and found["pid"] == proc.pid and found["status"] == "running"
        spawns = []
        monkeypatch.setattr(executor, "_spawn", lambda *a: spawns.append(a))
        assert executor.allocate(_spec(), "op_local2")["pid"] == proc.pid
        assert spawns == [], "the registered daemon is adopted, never doubled"
    finally:
        os.killpg(proc.pid, 9)
        proc.wait(timeout=5)


def test_local_retry_adopts_running_daemon(tmp_path) -> None:
    executor = LocalExecutor(tmp_path / "ex", extra_env={"OPENCODE_BIN": FAKE_OPENCODE})
    handles: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def go() -> None:
        try:
            handles.append(executor.allocate(_spec(), "op_local3"))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    try:
        assert not errors and len(handles) == 3
        assert len({h["pid"] for h in handles}) == 1, "concurrent retries share one daemon"
        again = executor.lookup("op_local3", None)
        assert again["pid"] == handles[0]["pid"] and again["status"] == "running"
        assert executor.connect_runtime(again, None).startswith("http://127.0.0.1:")
    finally:
        assert executor.terminate(handles[0], "cleanup", None)
    assert executor.lookup("op_local3", None)["status"] == "terminated"


# -------------------------------------------------------- unknown-outcome gate (item 5)
def _unknown_after_accepted_start(stack) -> tuple[Any, str, str]:
    """A preparing Turn whose start was accepted, then the runtime vanished."""
    principal = make_principal(stack.db)
    created = stack.sessions.create(
        principal, principal.default_workspace_id, session_body(message={"content": "go"})
    )
    sid, t1 = created["session_id"], created["turn_id"]

    def fn(u: Any) -> str:
        session = u.get("sessions", sid, lock=True)
        u.update_where("jobs", {"kind": "turn.dispatch"}, {"state": "cancelled"})
        u.update("turns", t1, {"state": "preparing"}, bump_version=True)
        u.update("sessions", sid, {"active_turn_id": t1})
        lease = u.insert(
            "executor_leases",
            {
                "id": new_id("lease"),
                "workspace_id": session["workspace_id"],
                "session_id": sid,
                "backend": "local",
                "allocation_operation_id": new_id("operation"),
                "generation": 1,
                "image_digest": "local:test",
                "resource_class": session["resource_class"],
                "state": "ready",
            },
        )
        execution_id = new_id("execution")
        u.insert(
            "executions",
            {
                "id": execution_id,
                "workspace_id": session["workspace_id"],
                "session_id": sid,
                "turn_id": t1,
                "attempt_ordinal": 1,
                "executor_lease_id": lease["id"],
                "operation_id": execution_id,
                "harness_provider": "opencode",
                "launch_evidence": "accepted",
            },
        )
        return execution_id

    execution_id = stack.db.run(fn)
    stack.db.run(lambda u: stack.execution._runtime_lost(u, execution_id, confirmed=True))
    return principal, sid, t1


def _dispatches(stack, turn_id: str) -> int:
    return stack.db.read(lambda u: u.count("jobs", {"kind": "turn.dispatch", "turn_id": turn_id}))


def test_failed_unknown_outcome_blocks_followups_until_acknowledged(stack) -> None:
    principal, sid, t1 = _unknown_after_accepted_start(stack)
    turn = stack.turn(t1)
    assert turn["state"] == "failed" and turn["reason"] == "outcome_unknown"
    t2 = stack.sessions.send(principal, sid, {"content": "next"})["turn_id"]
    assert _dispatches(stack, t2) == 0, "a failed unknown outcome blocks dispatch"
    stack.sessions.acknowledge_unknown(principal, t1)
    assert _dispatches(stack, t2) == 1


def test_stale_dispatch_job_cannot_bypass_unknown_gate(stack) -> None:
    principal, sid, t1 = _unknown_after_accepted_start(stack)
    t2 = stack.sessions.send(principal, sid, {"content": "next"})["turn_id"]
    stack.db.run(
        lambda u: u.enqueue_job(
            workspace_id=principal.default_workspace_id,
            kind="turn.dispatch",
            target_id=t2,
            session_id=sid,
        )
    )
    stack.worker.run_until_idle()
    assert stack.turn(t2)["state"] == "queued"
    [job] = _jobs(stack, "turn.dispatch")[-1:]
    assert job["state"] == "succeeded" and job["result"] == {"blocked": "outcome_unknown"}
    assert stack.db.read(lambda u: u.count("executor_leases", {"session_id": sid})) == 1
