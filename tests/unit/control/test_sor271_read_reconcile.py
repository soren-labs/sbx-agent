"""Slow sandbox reconciliation must not hold a public status read open."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from control.api_v1 import routes
from control.backend import SandboxPoll
from control.run_store import InMemoryRunStore, RunLedger
from tests.unit.control.test_sor268_async_perf import (
    _plane_with_blocking_backend,
    _PlaneStub,
    _session,
)


def test_read_reconcile_is_bounded_and_single_flight():
    plane, _backend, store = _plane_with_blocking_backend()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow_probe(sid):
        calls.append(sid)
        entered.set()
        release.wait(3)
        return True

    plane.reconcile_turn = slow_probe
    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(plane.maybe_reconcile_turn, "agent") for _ in range(20)]
            assert entered.wait(1)
            # No caller may wait for the stalled remote operation to end.
            until = time.monotonic() + 0.5
            while time.monotonic() < until and not all(f.done() for f in futures):
                time.sleep(0.01)
            assert all(f.done() for f in futures)
            assert calls == ["agent"]
    finally:
        release.set()


def test_parallel_reconcile_does_not_finish_same_turn_twice(monkeypatch):
    import control.service as service

    plane, backend, store = _plane_with_blocking_backend()
    backend.poll = lambda handle: SandboxPoll(alive=True, active_processes=1)
    sid = plane.create_session(owner="o", model="m", title="t")
    rec = store.get(sid)
    rec.status = "running"
    rec.current_turn_n = 1
    rec.current_turn_id = "turn-1"
    store.put(rec)
    entered, release = threading.Event(), threading.Event()
    finishes = []

    def evidence(*args):
        entered.set()
        release.wait(3)
        return {"n": 1}

    monkeypatch.setattr(service, "read_json", evidence)
    plane._finish_turn = lambda *args: finishes.append(args)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(plane.reconcile_turn, sid)
        assert entered.wait(1)
        second = pool.submit(plane.reconcile_turn, sid)
        try:
            assert second.result(timeout=0.5) is False
        finally:
            release.set()
        assert first.result(timeout=1) is True
    assert len(finishes) == 1


def test_stalled_read_reconciles_do_not_build_an_unbounded_queue():
    plane, _backend, _store = _plane_with_blocking_backend()
    release = threading.Event()
    calls = []

    def stalled(sid):
        calls.append(sid)
        release.wait(3)
        return False

    plane.reconcile_turn = stalled
    try:
        for n in range(20):
            assert plane.maybe_reconcile_turn(f"agent-{n}") is False
        assert len(calls) == 4
        assert len(plane._read_reconcile_pending) == 4
    finally:
        release.set()


def test_queued_and_creating_run_views_never_probe_sandbox(monkeypatch):
    ledger = RunLedger(InMemoryRunStore())
    plane = _PlaneStub(ledger)
    rec = _session("agent")
    rec.status = "creating"
    calls = []
    monkeypatch.setattr(routes, "_turn_payload", lambda *args: calls.append(args))
    for n, status in enumerate(("CREATING", "QUEUED"), 1):
        row = ledger.begin(agent_id=rec.id, n=n, status=status)
        view = routes._render_run(plane, plane.public(rec), rec, n, set(), record=row)
        assert view["status"] == status
    assert calls == []
