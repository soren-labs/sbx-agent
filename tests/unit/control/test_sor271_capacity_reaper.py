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
from control.app import create_app
from control.backend import LocalProcessBackend, SandboxSpec
from control.ports import Account
from control.reaper import reap, sweep_plane
from control.scheduler import (
    DEFAULT_MAX_GLOBAL,
    AccountScheduler,
    ScheduleRefused,
    session_running_source,
)
from control.service import ConcurrencyLimit, ControlPlane
from control.store import InMemoryStore, SessionRecord, empty_usage
from tests.fakes.fake_ports import InMemoryAccountRegistry

_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _record(
    *,
    session_id: str,
    status: str,
    handle_id: str | None = None,
    created: datetime = _NOW,
    last: datetime | None = None,
    owner: str = "sbx",
    account_id: str | None = None,
) -> SessionRecord:
    at = last if last is not None else created
    tags = {"session_id": session_id, "owner": owner}
    if account_id is not None:
        tags["account_id"] = account_id
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
        sandbox_tags=tags,
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


# --- SOR-271 round-4: capacity default + time-bounded invocation path ---
#
# Round-3 guarded *throwing* calls; the round-4 gate proved the wedge was
# calls that never *return* (a hung sandbox/Dict RPC starves the sweep
# identically) plus a divergent cap default: ``create_app`` fell back to
# ``MAX_CONCURRENT=2`` when ``SBX_MAX_CONCURRENT`` was absent remotely,
# while the scheduler fell back to ``DEFAULT_MAX_GLOBAL=8`` — so binds
# refused at exactly 2 live agents.


def test_plane_cap_fallback_matches_scheduler_global(monkeypatch) -> None:
    """Both caps resolve from the same knob: absent env → DEFAULT_MAX_GLOBAL
    (not the old plane-local 2), set env → honored by both."""
    monkeypatch.delenv("SBX_MAX_CONCURRENT", raising=False)
    app = create_app(
        backend=LocalProcessBackend(),
        store=InMemoryStore(),
        runner_cmd=["python", "-m", "runtime.runner"],
    )
    assert app.state.plane.max_concurrent == DEFAULT_MAX_GLOBAL
    assert DEFAULT_MAX_GLOBAL == 8

    monkeypatch.setenv("SBX_MAX_CONCURRENT", "4")
    app = create_app(
        backend=LocalProcessBackend(),
        store=InMemoryStore(),
        runner_cmd=["python", "-m", "runtime.runner"],
    )
    assert app.state.plane.max_concurrent == 4


def test_close_releases_the_capacity_slot() -> None:
    """Closing an agent frees its slot: cap counts genuinely-live only."""
    backend, store = LocalProcessBackend(), InMemoryStore()
    plane = _plane(store, backend, cap=1)
    sid = _open(plane)
    plane.close(sid)
    assert plane.get(sid).status == "closed"
    assert _open(plane)


def test_running_source_skips_dead_weight_records() -> None:
    """The scheduler's store-derived count must exclude the records the
    reaper owns as ``lost`` — stale ``creating``/``running`` zombies —
    while still counting fresh creates, live runs, idle and suspended."""
    store = InMemoryStore()
    now = datetime.now(UTC)
    stale = now - timedelta(seconds=3600)
    store.put(_record(session_id="c-stale", status="creating", created=stale, account_id="a"))
    bound_stale = _record(
        session_id="c-stale-bound",
        status="creating",
        handle_id="sbx-cb",
        last=stale,
        account_id="a",
    )
    store.put(bound_stale)
    store.put(_record(session_id="r-stale", status="running", last=stale, account_id="a"))
    store.put(_record(session_id="c-fresh", status="creating", created=now, account_id="a"))
    store.put(_record(session_id="r-live", status="running", last=now, account_id="a"))
    store.put(_record(session_id="i-live", status="idle", last=now, account_id="a"))
    store.put(_record(session_id="s-live", status="suspended", last=stale, account_id="a"))
    store.put(_record(session_id="term", status="closed", last=now, account_id="a"))
    store.put(_record(session_id="other", status="idle", last=now, account_id="b"))
    count = session_running_source(store)
    assert count("a") == 4  # fresh creating + live running + idle + suspended
    assert count("b") == 1
    assert count("nobody") == 0


def test_scheduler_acquire_unwedged_by_dead_weight() -> None:
    """Dead-weight records must not saturate ``max_global``: 3 stale
    ``creating`` zombies under a global cap of 1 still let a bind land;
    a live lease still consumes the slot honestly."""
    registry = InMemoryAccountRegistry()
    registry.put(
        Account(
            id="acct",
            provider="codex",
            label="acct",
            status="active",
            max_concurrent=1,
            created_at=datetime.now(UTC).isoformat(),
        )
    )
    store = InMemoryStore()
    stale = datetime.now(UTC) - timedelta(seconds=3600)
    for i in range(3):
        store.put(_record(session_id=f"z{i}", status="creating", created=stale, account_id="acct"))
    scheduler = AccountScheduler(
        registry, max_global=1, external_running=session_running_source(store)
    )
    lease = scheduler.acquire(provider="codex", account="acct")
    with pytest.raises(ScheduleRefused):
        scheduler.acquire(provider="codex", account="acct")
    lease.release()
    assert scheduler.acquire(provider="codex", account="acct")


def test_reap_skips_record_with_hanging_poll(monkeypatch) -> None:
    """One never-returning ``backend.poll`` skips that record — it must
    not starve the rest of the sweep (the production wedge)."""
    monkeypatch.setattr(reaper, "_POLL_BOUND_S", 0.05)
    backend, store = LocalProcessBackend(), InMemoryStore()
    wedge = _sandbox(backend, "wedge")
    ok = _sandbox(backend, "ok")
    last = _NOW - timedelta(seconds=3600)
    store.put(_record(session_id="wedge", status="idle", handle_id=wedge.id, last=last))
    store.put(_record(session_id="ok", status="idle", handle_id=ok.id, last=last))

    class WedgedBackend:
        def poll(self, handle):  # noqa: ANN001 - test double
            if handle.id == wedge.id:
                threading.Event().wait()  # never returns
            return backend.poll(handle)

        def __getattr__(self, name: str):
            return getattr(backend, name)

    started = time.monotonic()
    actions = reap(store, WedgedBackend(), _NOW, idle_timeout_s=300)
    assert time.monotonic() - started < 30
    assert store.get("wedge").status == "idle"  # UNKNOWN — retried next sweep
    assert store.get("ok").status == "timed_out"
    kinds = [a.kind for a in actions]
    assert "reap_error" in kinds
    assert "timed_out" in kinds


def test_reap_deadline_exits_the_sweep_early() -> None:
    """``deadline_s`` is the soft wall for one tick: an exhausted deadline
    emits ``sweep_deadline`` and returns instead of running over the
    cron period into the next invocation."""
    backend, store = LocalProcessBackend(), InMemoryStore()
    last = _NOW - timedelta(seconds=3600)
    for i in range(3):
        h = _sandbox(backend, f"d{i}")
        store.put(_record(session_id=f"d{i}", status="idle", handle_id=h.id, last=last))
    actions = reap(store, backend, _NOW, idle_timeout_s=300, deadline_s=0)
    assert [a.kind for a in actions].count("sweep_deadline") == 1
    assert all(r.status == "idle" for r in store.list_all())


def test_sweep_plane_survives_hanging_reconcile() -> None:
    """The cron body itself: a ``reconcile_turns`` that never returns is
    time-bounded, and the tick still reaches ``reap()`` — the round-4
    fix for a cron that fired yet never reaped."""
    backend, store = LocalProcessBackend(), InMemoryStore()
    plane = _plane(store, backend, cap=8)
    h = _sandbox(backend, "z1")
    store.put(
        _record(
            session_id="z1",
            status="idle",
            handle_id=h.id,
            last=_NOW - timedelta(seconds=3600),
        )
    )
    plane.reconcile_turns = lambda: threading.Event().wait()  # type: ignore[method-assign]
    logs: list[str] = []
    summary = sweep_plane(plane, reconcile_bound_s=0.05, sweep_bound_s=30, log=logs.append)
    assert store.get("z1").status == "timed_out"
    assert backend.poll(h).alive is False
    assert summary["action_kinds"].get("timed_out") == 1
    assert any("reconcile exceeded" in line for line in logs)
    assert any("tick done" in line for line in logs)


def test_sweep_plane_happy_path_reaps_and_logs() -> None:
    """sweep_plane wires reconcile → reap → lease/settle callbacks the
    way the deployed cron does; an expired idle ends ``timed_out`` and
    terminated, and the summary/logs prove invocation."""
    backend, store = LocalProcessBackend(), InMemoryStore()
    plane = _plane(store, backend, cap=8)
    h = _sandbox(backend, "idle1")
    store.put(
        _record(
            session_id="idle1",
            status="idle",
            handle_id=h.id,
            last=_NOW - timedelta(seconds=3600),
        )
    )
    logs: list[str] = []
    summary = sweep_plane(plane, log=logs.append)
    assert store.get("idle1").status == "timed_out"
    assert backend.poll(h).alive is False
    assert summary["action_kinds"] == {"timed_out": 1}
    assert logs[0].startswith("[reap] tick start")
    assert logs[-1].startswith("[reap] tick done")
