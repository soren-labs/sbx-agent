"""SOR-80 lifecycle hardening: reaper create-window safety, terminal cleanup
retry, lease release hooks, post_message creating rejection + rollback, and
close/create terminate-failure ordering."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from control.api_v1.state import V1State
from control.backend import LocalProcessBackend, SandboxHandle, SandboxSpec
from control.reaper import reap
from control.service import (
    ControlPlane,
    SessionConflict,
    release_lease,
    release_lease_for_action,
)
from control.store import InMemoryStore, SessionRecord, empty_usage

RUNNER = [
    sys.executable,
    str(Path(__file__).resolve().parents[3] / "tests" / "fakes" / "stub_runner.py"),
]


def _now() -> datetime:
    return datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _record(
    *,
    session_id: str,
    status: str,
    handle: SandboxHandle | None,
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
        sandbox_id=handle.id if handle else None,
        sandbox_root=str(handle.root) if handle else None,
        sandbox_tags=dict(handle.tags) if handle else {},
        last_activity_at=last,
    )


def _plane(backend, store):
    return ControlPlane(backend, store, RUNNER, clock=_now)


class _FlakyBackend:
    """Wrap LocalProcessBackend; fails the next ``fail_terminate`` calls."""

    def __init__(self, fail_terminate: int = 0) -> None:
        self.inner = LocalProcessBackend()
        self.fail_terminate = fail_terminate
        self.terminated: list[str] = []

    def create(self, spec):
        return self.inner.create(spec)

    def exec(self, handle, argv, env=None):
        return self.inner.exec(handle, argv, env)

    def terminate(self, handle):
        self.terminated.append(handle.id)
        if self.fail_terminate > 0:
            self.fail_terminate -= 1
            raise RuntimeError("terminate failed")
        return self.inner.terminate(handle)

    def poll(self, handle):
        return self.inner.poll(handle)

    def list(self, tags=None):
        return self.inner.list(tags)


class _FakeLease:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


# ------------------------------------------------------- reaper create window


def test_in_flight_create_is_not_orphan_killed() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    created = _now()
    handle = backend.create(SandboxSpec(tags={"session_id": "s1", "owner": "sbx"}))
    store.put(
        SessionRecord(
            id="s1",
            title="t",
            status="creating",
            created_at=created,
            updated_at=created,
            model="gpt-5.6-luna",
            turns=0,
            usage=empty_usage(),
            messages=[],
            owner="sbx",
            sandbox_tags={"session_id": "s1", "owner": "sbx"},
            last_activity_at=created,
        )
    )
    actions = reap(store, backend, created + timedelta(seconds=5), create_grace_s=300)
    rec = store.get("s1")
    assert rec is not None
    assert rec.status == "creating"
    assert backend.poll(handle).alive is True
    assert actions == []
    backend.terminate(handle)


def test_stale_unbound_create_marks_lost_and_orphan_cleaned() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    created = _now()
    handle = backend.create(SandboxSpec(tags={"session_id": "s1", "owner": "sbx"}))
    store.put(
        SessionRecord(
            id="s1",
            title="t",
            status="creating",
            created_at=created,
            updated_at=created,
            model="gpt-5.6-luna",
            turns=0,
            usage=empty_usage(),
            messages=[],
            owner="sbx",
            sandbox_tags={"session_id": "s1", "owner": "sbx"},
            last_activity_at=created,
        )
    )
    actions = reap(store, backend, created + timedelta(seconds=301), create_grace_s=300)
    rec = store.get("s1")
    assert rec is not None
    assert rec.status == "lost"
    assert backend.poll(handle).alive is False
    kinds = {(a.kind, a.session_id) for a in actions}
    assert ("lost", "s1") in kinds
    assert ("orphan_terminate", None) in kinds


# ------------------------------------------------------- terminal cleanup retry


def test_terminal_record_live_sandbox_is_cleaned_up() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    created = _now()
    handle = backend.create(SandboxSpec(tags={"session_id": "s1", "owner": "sbx"}))
    store.put(_record(session_id="s1", status="closed", handle=handle, last=created))
    actions = reap(store, backend, created + timedelta(seconds=5))
    assert backend.poll(handle).alive is False
    assert any(
        a.kind == "terminal_cleanup" and a.session_id == "s1" and a.sandbox_id == handle.id
        for a in actions
    )
    # Second pass: nothing left to clean.
    assert reap(store, backend, created + timedelta(seconds=10)) == []


def test_terminal_cleanup_retries_after_terminate_failure() -> None:
    backend = _FlakyBackend(fail_terminate=1)
    store = InMemoryStore()
    created = _now()
    handle = backend.create(SandboxSpec(tags={"session_id": "s1", "owner": "sbx"}))
    store.put(_record(session_id="s1", status="closed", handle=handle, last=created))

    first = reap(store, backend, created + timedelta(seconds=5))
    assert backend.poll(handle).alive is True
    assert any(a.kind == "cleanup_failed" and a.session_id == "s1" for a in first)

    second = reap(store, backend, created + timedelta(seconds=10))
    assert backend.poll(handle).alive is False
    assert any(a.kind == "terminal_cleanup" and a.session_id == "s1" for a in second)


# ------------------------------------------------------------- lease release


def test_release_lease_is_idempotent() -> None:
    v1 = V1State()
    lease = _FakeLease()
    v1.set_lease("s1", lease)
    release_lease(v1, "s1")
    release_lease(v1, "s1")
    assert lease.released is True
    assert v1.pop_lease("s1") is None


def test_reaper_hook_releases_lease_on_terminal_actions() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    created = _now()
    handle = backend.create(SandboxSpec(tags={"session_id": "s1", "owner": "sbx"}))
    store.put(_record(session_id="s1", status="idle", handle=handle, last=created))
    backend.terminate(handle)

    v1 = V1State()
    lease = _FakeLease()
    v1.set_lease("s1", lease)
    actions = reap(
        store,
        backend,
        created + timedelta(seconds=5),
        on_action=lambda action: release_lease_for_action(v1, action),
    )
    assert any(a.kind == "timed_out" and a.session_id == "s1" for a in actions)
    assert lease.released is True
    assert v1.pop_lease("s1") is None


# --------------------------------------------------------- post_message rules


def test_post_message_rejects_creating_session() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    created = _now()
    handle = backend.create(SandboxSpec(tags={"session_id": "s1", "owner": "sbx"}))
    store.put(_record(session_id="s1", status="creating", handle=handle, last=created))
    plane = _plane(backend, store)
    with pytest.raises(SessionConflict) as exc:
        plane.post_message("s1", "hi")
    assert exc.value.error == "session_not_runnable"
    backend.terminate(handle)


def test_post_message_rolls_back_to_idle_on_exec_failure() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    plane = _plane(backend, store)
    sid = plane.create_session(owner="sbx", title="t", model=None)
    assert store.get(sid).status == "idle"
    rec = store.get(sid)
    handle = rec.handle()
    assert handle is not None

    def _boom(h, argv, env=None):
        raise RuntimeError("exec boom")

    plane.backend.exec = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        plane.post_message(sid, "hello")
    rec = store.get(sid)
    assert rec is not None
    assert rec.status == "idle"
    assert rec.current_turn_id is None
    assert rec.current_turn_n is None
    assert rec.messages == []
    backend.terminate(handle)


def test_post_message_rolls_back_to_lost_when_sandbox_gone() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    plane = _plane(backend, store)
    sid = plane.create_session(owner="sbx", title="t", model=None)
    rec = store.get(sid)
    handle = rec.handle()
    assert handle is not None
    backend.terminate(handle)

    with pytest.raises(Exception):
        plane.post_message(sid, "hello")
    rec = store.get(sid)
    assert rec is not None
    assert rec.status == "lost"
    assert rec.ended_at is not None
    assert rec.current_turn_id is None
    assert rec.messages == []


# ------------------------------------------------- terminate-failure ordering


def test_close_survives_terminate_failure_then_reaper_cleans() -> None:
    backend = _FlakyBackend(fail_terminate=1)
    store = InMemoryStore()
    plane = _plane(backend, store)
    sid = plane.create_session(owner="sbx", title="t", model=None)
    rec = store.get(sid)
    handle = rec.handle()
    assert handle is not None

    closed = plane.close(sid)  # terminate fails once; close still succeeds
    assert closed.status == "closed"
    assert backend.poll(handle).alive is True

    actions = reap(store, backend, _now() + timedelta(seconds=5))
    assert backend.poll(handle).alive is False
    assert any(a.kind == "terminal_cleanup" and a.session_id == sid for a in actions)


def test_failed_create_leaves_terminal_bound_record_for_reaper() -> None:
    class _InitFailBackend(_FlakyBackend):
        def exec(self, handle, argv, env=None):
            raise RuntimeError("init exec boom")

    backend = _InitFailBackend(fail_terminate=1)
    store = InMemoryStore()
    plane = _plane(backend, store)
    with pytest.raises(RuntimeError):
        plane.create_session(owner="sbx", title="t", model=None)
    (rec,) = store.list_all()
    assert rec.status == "lost"
    assert rec.sandbox_id is not None  # bound for later cleanup
    # terminate inside create consumed the one failure → sandbox still alive
    handle = SandboxHandle(id=rec.sandbox_id, root=Path(rec.sandbox_root), tags=rec.sandbox_tags)
    assert backend.poll(handle).alive is True
    actions = reap(store, backend, _now() + timedelta(seconds=5))
    assert backend.poll(handle).alive is False
    assert any(a.kind == "terminal_cleanup" and a.session_id == rec.id for a in actions)
