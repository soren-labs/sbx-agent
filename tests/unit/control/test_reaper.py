"""Reaper: injectable clock, idle timeout, lost, orphan."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from control.accounts import InMemoryAccountStore, PersistentAccountRegistry
from control.backend import LocalProcessBackend, SandboxSpec
from control.ports import Account
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


class TestAccountSweep:
    """SOR-63: expired account cooldowns recover proactively on the sweep."""

    def _registry(self) -> PersistentAccountRegistry:
        return PersistentAccountRegistry(InMemoryAccountStore())

    def test_expired_cooldown_recovers_to_active(self) -> None:
        backend = LocalProcessBackend()
        store = InMemoryStore()
        registry = self._registry()
        registry.put(
            Account(
                id="agy-1",
                provider="antigravity",
                label="agy-1",
                status="cooling",
                cooldown_until="2026-09-13T11:30:00+00:00",
                last_error="rate_limited",
            )
        )
        actions = reap(store, backend, _now(), account_registry=registry)
        acct = registry.get("agy-1")
        assert acct is not None and acct.status == "active"
        assert acct.cooldown_until is None
        assert any(a.kind == "account_recovered" and a.account_id == "agy-1" for a in actions)

    def test_unexpired_cooldown_left_alone(self) -> None:
        backend = LocalProcessBackend()
        store = InMemoryStore()
        registry = self._registry()
        registry.put(
            Account(
                id="grok-1",
                provider="grok",
                label="grok-1",
                status="cooling",
                cooldown_until="2026-09-13T13:00:00+00:00",
            )
        )
        actions = reap(store, backend, _now(), account_registry=registry)
        acct = registry.get("grok-1")
        assert acct is not None and acct.status == "cooling"
        assert not any(a.kind == "account_recovered" for a in actions)

    def test_non_cooling_statuses_untouched(self) -> None:
        backend = LocalProcessBackend()
        store = InMemoryStore()
        registry = self._registry()
        for account_id, status in (("a1", "active"), ("a2", "invalid"), ("a3", "disabled")):
            registry.put(Account(id=account_id, provider="grok", label=account_id, status=status))
        actions = reap(store, backend, _now(), account_registry=registry)
        assert not any(a.kind == "account_recovered" for a in actions)
        assert registry.get("a2").status == "invalid"  # type: ignore[union-attr]

    def test_no_registry_keeps_prior_behavior(self) -> None:
        backend = LocalProcessBackend()
        store = InMemoryStore()
        assert reap(store, backend, _now()) == []
