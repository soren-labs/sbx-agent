"""SOR-271 round-3 repair invariants: capacity accounting + reaper ownership.

Pins the two production-gate findings on exact main ``025d9504``:

1. ``open_session`` capacity must count only *genuinely live bound
   agents* plus *fresh* in-flight creates. Orphaned sandboxes (a leaked
   abandoned-bind create, a terminal record's surviving handle, or one
   with no record at all) bill money but hold no agent — counting them
   collapsed effective headroom to ~2 of ``SBX_MAX_CONCURRENT=8`` slots.
   A ``creating`` record older than ``create_grace_s`` is dead weight
   (its provisioner is gone; the reaper owns the ``lost`` transition)
   and must not hold a slot between sweeps either.

2. The cron invocation path is failure-isolated: ``reconcile_turns``
   runs before ``reap()`` every tick, so a throwing store read inside it
   killed the whole tick before reaping — and inside the sweep, one
   throwing ``on_action`` callback (the cron settles orphaned runs via
   the remote run ledger) aborted the records loop and skipped the
   orphan pass that reclaims zombie sandboxes. A never-returning
   checkpoint suspend starved identically.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from control import reaper
from control.backend import LocalProcessBackend, SandboxSpec
from control.reaper import reap
from control.service import ConcurrencyLimit, ControlPlane
from control.store import InMemoryStore, SessionRecord, empty_usage

_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _record(
    *,
    session_id: str,
    status: str,
    handle_id: str | None = None,
    created: datetime = _NOW,
    last: datetime | None = None,
    owner: str = "sbx",
) -> SessionRecord:
    at = last if last is not None else created
    return SessionRecord(
        id=session_id,
        title="t",
        status=status,
        created_at=created,
        updated_at=at,
        model="m",
        turns=0,
        usage=empty_usage(),
        messages=[],
        owner=owner,
        sandbox_id=handle_id,
        sandbox_root="/tmp/x" if handle_id else None,
        sandbox_tags={"session_id": session_id, "owner": owner} if handle_id else {},
        last_activity_at=at,
    )


def _plane(store: InMemoryStore, backend: LocalProcessBackend, cap: int) -> ControlPlane:
    return ControlPlane(backend, store, ["fake-runner"], max_concurrent=cap, clock=lambda: _NOW)


def _open(plane: ControlPlane) -> str:
    return plane.open_session(owner="sbx", title=None, model=None)


def _sandbox(backend: LocalProcessBackend, session_id: str, owner: str = "sbx"):
    return backend.create(SandboxSpec(tags={"session_id": session_id, "owner": owner}))


def test_orphan_sandbox_without_record_holds_no_capacity() -> None:
    backend, store = LocalProcessBackend(), InMemoryStore()
    plane = _plane(store, backend, cap=1)
    # Leaked abandoned-bind sandbox: live, tagged, but no session record
    # backs it (reaper's orphan pass owns the cleanup, not the cap).
    _sandbox(backend, "ghost")
    assert _open(plane)


def test_terminal_record_sandbox_holds_no_capacity() -> None:
    backend, store = LocalProcessBackend(), InMemoryStore()
    plane = _plane(store, backend, cap=1)
    handle = _sandbox(backend, "dead")
    store.put(_record(session_id="dead", status="lost", handle_id=handle.id))
    assert _open(plane)


def test_suspended_record_sandbox_holds_no_capacity() -> None:
    backend, store = LocalProcessBackend(), InMemoryStore()
    plane = _plane(store, backend, cap=1)
    handle = _sandbox(backend, "susp")
    store.put(_record(session_id="susp", status="suspended", handle_id=handle.id))
    assert _open(plane)


def test_live_bound_agent_still_consumes_capacity() -> None:
    backend, store = LocalProcessBackend(), InMemoryStore()
    plane = _plane(store, backend, cap=1)
    handle = _sandbox(backend, "bound")
    store.put(_record(session_id="bound", status="idle", handle_id=handle.id))
    with pytest.raises(ConcurrencyLimit):
        _open(plane)


def test_bound_record_matched_by_sandbox_id_when_tag_missing() -> None:
    backend, store = LocalProcessBackend(), InMemoryStore()
    plane = _plane(store, backend, cap=1)
    handle = backend.create(SandboxSpec(tags={"owner": "sbx"}))
    store.put(_record(session_id="bound2", status="running", handle_id=handle.id))
    with pytest.raises(ConcurrencyLimit):
        _open(plane)


def test_stale_creating_record_holds_no_capacity() -> None:
    backend, store = LocalProcessBackend(), InMemoryStore()
    plane = _plane(store, backend, cap=1)
    # Provisioner died mid-bind hours ago; the reaper marks this ``lost``
    # on the next sweep — it must not hold a slot in between.
    stale = _record(session_id="wedged", status="creating", created=_NOW - timedelta(hours=1))
    store.put(stale)
    assert _open(plane)


def test_fresh_creating_record_still_consumes_capacity() -> None:
    backend, store = LocalProcessBackend(), InMemoryStore()
    plane = _plane(store, backend, cap=1)
    store.put(_record(session_id="inflight", status="creating", created=_NOW))
    with pytest.raises(ConcurrencyLimit):
        _open(plane)


def test_other_owner_records_and_sandboxes_are_out_of_scope() -> None:
    backend, store = LocalProcessBackend(), InMemoryStore()
    plane = _plane(store, backend, cap=1)
    other = backend.create(SandboxSpec(tags={"session_id": "x", "owner": "other"}))
    store.put(_record(session_id="x", status="idle", handle_id=other.id, owner="other"))
    store.put(_record(session_id="y", status="creating", created=_NOW, owner="other"))
    assert _open(plane)


def test_on_action_failure_never_aborts_the_sweep() -> None:
    backend, store = LocalProcessBackend(), InMemoryStore()
    last = _NOW - timedelta(seconds=3600)
    records = []
    for i in range(3):
        h = _sandbox(backend, f"z{i}")
        rec = _record(session_id=f"z{i}", status="idle", handle_id=h.id, last=last)
        store.put(rec)
        records.append(rec)
    orphan = _sandbox(backend, "orphan")

    def boom(action) -> None:
        raise RuntimeError("run ledger down")

    actions = reap(
        store,
        backend,
        _NOW,
        idle_timeout_s=300,
        checkpoints=None,
        on_action=boom,
    )
    # Every idle record still terminalized + terminated, the orphan pass
    # still ran, and each callback failure is recorded as a reap_error.
    stored = {r.id: r for r in store.list_all()}
    assert all(stored[r.id].status == "timed_out" for r in records)
    assert all(backend.poll(r.handle()).alive is False for r in records)
    assert backend.poll(orphan).alive is False
    kinds = [a.kind for a in actions]
    assert kinds.count("timed_out") == 3
    assert "orphan_terminate" in kinds
    assert kinds.count("reap_error") >= 3


def test_hanging_suspend_falls_back_to_timed_out(monkeypatch) -> None:
    monkeypatch.setattr(reaper, "_SUSPEND_BOUND_S", 0.1)
    backend, store = LocalProcessBackend(), InMemoryStore()
    h = _sandbox(backend, "s1")
    rec = _record(
        session_id="s1",
        status="idle",
        handle_id=h.id,
        last=_NOW - timedelta(seconds=3600),
    )
    store.put(rec)

    class HangingCheckpoints:
        def suspend(self, _rec, _handle) -> bool:
            threading.Event().wait()  # never returns — wedged remote op

    started = time.monotonic()
    reap(store, backend, _NOW, idle_timeout_s=300, checkpoints=HangingCheckpoints())
    assert time.monotonic() - started < 30
    assert store.get("s1").status == "timed_out"
    assert backend.poll(h).alive is False


def test_reconcile_turns_survives_store_listing_failure() -> None:
    class BrokenStore(InMemoryStore):
        def list_all(self) -> list[SessionRecord]:
            raise RuntimeError("dict unreachable")

    plane = _plane(BrokenStore(), LocalProcessBackend(), cap=8)
    assert plane.reconcile_turns() == []


def test_reconcile_turns_skips_one_bad_record() -> None:
    backend, store = LocalProcessBackend(), InMemoryStore()
    plane = _plane(store, backend, cap=8)
    for sid in ("bad", "good"):
        store.put(_record(session_id=sid, status="running"))

    original = plane.reconcile_turn

    def flaky(session_id: str) -> bool:
        if session_id == "bad":
            raise RuntimeError("poll wedge")
        return original(session_id)

    plane.reconcile_turn = flaky  # type: ignore[method-assign]
    assert plane.reconcile_turns() == []
