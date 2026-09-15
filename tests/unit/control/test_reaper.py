"""Reaper: injectable clock, idle timeout, lost, orphan."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from control.backend import LocalProcessBackend, SandboxSpec
from control.reaper import reap
from control.store import InMemoryStore, SessionRecord, empty_usage


def _now() -> datetime:
    return datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _record(
    *,
    session_id: str,
    status: str,
    handle_id: str | None,
    root: str | None,
    last: datetime,
    owner: str = "sbx",
) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        title="t",
        status=status,
        created_at=last,
        updated_at=last,
        model="gpt-5.6-luna",
        turns=0,
        usage=empty_usage(),
        messages=[],
        owner=owner,
        sandbox_id=handle_id,
        sandbox_root=root,
        sandbox_tags={"session_id": session_id, "owner": owner} if handle_id else {},
        last_activity_at=last,
    )


def test_idle_timeout_terminates_and_marks_timed_out() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    handle = backend.create(SandboxSpec(tags={"session_id": "s1", "owner": "sbx"}))
    created = _now()
    store.put(
        _record(
            session_id="s1",
            status="idle",
            handle_id=handle.id,
            root=str(handle.root),
            last=created,
        )
    )
    later = created + timedelta(minutes=31)
    actions = reap(store, backend, later, idle_timeout_s=1800)
    rec = store.get("s1")
    assert rec is not None
    assert rec.status == "timed_out"
    assert rec.ended_at == later
    assert backend.poll(handle).alive is False
    assert any(a.kind == "timed_out" and a.session_id == "s1" for a in actions)


def test_idle_under_threshold_is_left_alone() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    handle = backend.create(SandboxSpec(tags={"session_id": "s1", "owner": "sbx"}))
    created = _now()
    store.put(
        _record(
            session_id="s1",
            status="idle",
            handle_id=handle.id,
            root=str(handle.root),
            last=created,
        )
    )
    actions = reap(store, backend, created + timedelta(minutes=10), idle_timeout_s=1800)
    rec = store.get("s1")
    assert rec is not None
    assert rec.status == "idle"
    assert backend.poll(handle).alive is True
    assert actions == []
    backend.terminate(handle)


def test_missing_sandbox_while_idle_is_timed_out() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    handle = backend.create(SandboxSpec(tags={"session_id": "s1", "owner": "sbx"}))
    created = _now()
    store.put(
        _record(
            session_id="s1",
            status="idle",
            handle_id=handle.id,
            root=str(handle.root),
            last=created,
        )
    )
    backend.terminate(handle)
    actions = reap(store, backend, created + timedelta(seconds=5), idle_timeout_s=1800)
    rec = store.get("s1")
    assert rec is not None
    assert rec.status == "timed_out"
    assert any(a.kind == "timed_out" for a in actions)


def test_missing_sandbox_while_running_is_lost() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    handle = backend.create(SandboxSpec(tags={"session_id": "s2", "owner": "sbx"}))
    created = _now()
    store.put(
        _record(
            session_id="s2",
            status="running",
            handle_id=handle.id,
            root=str(handle.root),
            last=created,
        )
    )
    backend.terminate(handle)
    actions = reap(store, backend, created + timedelta(seconds=5), idle_timeout_s=1800)
    rec = store.get("s2")
    assert rec is not None
    assert rec.status == "lost"
    assert any(a.kind == "lost" and a.session_id == "s2" for a in actions)


def test_stale_bound_creating_record_is_lost_and_sandbox_reclaimed() -> None:
    """Provisioner gone mid-create (control-plane restart): a ``creating``
    record with a live bound sandbox can never settle — past the create
    grace window it must go ``lost`` and the orphan pass reclaims the
    sandbox (SOR-82 review)."""
    backend = LocalProcessBackend()
    store = InMemoryStore()
    handle = backend.create(SandboxSpec(tags={"session_id": "s3", "owner": "sbx"}))
    created = _now()
    store.put(
        _record(
            session_id="s3",
            status="creating",
            handle_id=handle.id,
            root=str(handle.root),
            last=created,
        )
    )
    actions = reap(store, backend, created + timedelta(seconds=301), create_grace_s=300)
    rec = store.get("s3")
    assert rec is not None
    assert rec.status == "lost"
    assert rec.ended_at == created + timedelta(seconds=301)
    assert any(a.kind == "lost" and a.session_id == "s3" for a in actions)
    # The same sweep reclaims the now-terminal record's sandbox.
    assert backend.poll(handle).alive is False
    assert any(a.kind == "terminal_cleanup" and a.sandbox_id == handle.id for a in actions)


def test_young_bound_creating_record_is_left_alone() -> None:
    """A bound ``creating`` record inside the grace window is an in-flight
    provision — the reaper must not touch it or its sandbox."""
    backend = LocalProcessBackend()
    store = InMemoryStore()
    handle = backend.create(SandboxSpec(tags={"session_id": "s4", "owner": "sbx"}))
    created = _now()
    store.put(
        _record(
            session_id="s4",
            status="creating",
            handle_id=handle.id,
            root=str(handle.root),
            last=created,
        )
    )
    actions = reap(store, backend, created + timedelta(seconds=60), create_grace_s=300)
    rec = store.get("s4")
    assert rec is not None
    assert rec.status == "creating"
    assert backend.poll(handle).alive is True
    assert actions == []
    backend.terminate(handle)


def test_orphan_sandbox_terminated_when_dict_record_missing() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    orphan = backend.create(SandboxSpec(tags={"owner": "sbx"}))
    assert backend.poll(orphan).alive is True
    actions = reap(store, backend, _now())
    assert backend.poll(orphan).alive is False
    assert any(a.kind == "orphan_terminate" and a.sandbox_id == orphan.id for a in actions)


def test_closed_records_are_not_reaped() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    created = _now()
    store.put(
        _record(
            session_id="closed",
            status="closed",
            handle_id=None,
            root=None,
            last=created,
        )
    )
    actions = reap(store, backend, created + timedelta(hours=2))
    rec = store.get("closed")
    assert rec is not None
    assert rec.status == "closed"
    assert actions == []
