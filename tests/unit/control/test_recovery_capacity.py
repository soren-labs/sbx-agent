"""Recovery must reserve capacity before suspended leases can be freed."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from control.app import create_app
from control.backend import LocalProcessBackend, SandboxSpec
from control.ports import Account
from control.reaper import reap
from control.scheduler import AccountScheduler, ScheduleRefused
from control.service import ConcurrencyLimit, SessionConflict
from control.store import InMemoryStore, SessionRecord
from tests.fakes.fake_ports import InMemoryAccountRegistry


@pytest.fixture
def env():
    backend, store = LocalProcessBackend(), InMemoryStore()
    app = create_app(backend=backend, store=store, runner_cmd=["unused"], max_concurrent=8)
    registry = InMemoryAccountRegistry()
    registry.put(Account(id="acct", provider="codex", label="test", max_concurrent=1))
    scheduler = AccountScheduler(
        registry,
        max_global=8,
        external_running=lambda aid: sum(
            r.status in {"creating", "running", "idle"} and r.sandbox_tags.get("account_id") == aid
            for r in store.list_all()
        ),
    )
    app.state.account_registry = registry
    app.state.scheduler = scheduler
    now = datetime.now(UTC)
    store.put(
        SessionRecord(
            id="suspended",
            title="test",
            status="suspended",
            created_at=now - timedelta(days=1),
            updated_at=now,
            model="test",
            turns=0,
            usage=None,
            messages=[],
            owner="owner",
            sandbox_tags={
                "session_id": "suspended",
                "owner": "owner",
                "provider": "codex",
                "account_id": "acct",
            },
        )
    )

    class Checkpoints:
        calls = 0

        def restore(self, rec):
            self.calls += 1
            return backend.create(SandboxSpec(tags=rec.sandbox_tags))

    checkpoints = Checkpoints()
    app.state.plane.checkpoints = checkpoints
    yield app, backend, store, scheduler, checkpoints
    for handle in backend.list():
        backend.terminate(handle)


def test_restored_session_acquires_and_keeps_original_account_lease(env):
    app, _backend, store, scheduler, checkpoints = env
    app.state.plane.recover_session("suspended")
    assert store.get("suspended").status == "idle"
    assert "suspended" in app.state.v1_state.leases
    assert scheduler.active_count == 1
    app.state.plane.recover_session("suspended")
    assert checkpoints.calls == 1


def test_recovery_refuses_when_original_account_is_full(env):
    app, _backend, store, scheduler, checkpoints = env
    lease = scheduler.acquire(provider="codex", account="acct")
    with pytest.raises(SessionConflict, match="account_busy"):
        app.state.plane.recover_session("suspended")
    assert checkpoints.calls == 0
    assert store.get("suspended").status == "suspended"
    lease.release()


def test_recovery_failure_releases_new_lease(env):
    app, _backend, store, scheduler, checkpoints = env

    class Retryable(Exception):
        retryable = True

    def fail(_rec):
        raise Retryable()

    checkpoints.restore = fail
    with pytest.raises(SessionConflict):
        app.state.plane.recover_session("suspended")
    assert store.get("suspended").status == "suspended"
    assert scheduler.active_count == 0
    assert "suspended" not in app.state.v1_state.leases


def test_inflight_restore_reserves_global_capacity_and_survives_reaper(env):
    app, backend, store, _scheduler, checkpoints = env
    plane = app.state.plane
    plane.max_concurrent = 1
    entered, finish = threading.Event(), threading.Event()
    original = checkpoints.restore

    def slow(rec):
        entered.set()
        assert finish.wait(5)
        return original(rec)

    checkpoints.restore = slow
    errors = []

    def recover():
        try:
            plane.recover_session("suspended")
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=recover)
    thread.start()
    assert entered.wait(2)
    try:
        with pytest.raises(ConcurrencyLimit):
            plane.open_session(owner="owner", title="new", model=None)
        plane.recover_session("suspended")
        assert reap(store, backend, datetime.now(UTC)) == []
        assert store.get("suspended").status == "creating"
    finally:
        finish.set()
        thread.join(5)
    assert not errors
    assert checkpoints.calls == 1
    assert store.get("suspended").status == "idle"


def test_recovery_refuses_when_global_capacity_is_full(env):
    app, backend, store, _scheduler, checkpoints = env
    plane = app.state.plane
    plane.max_concurrent = 1
    handle = backend.create(SandboxSpec(tags={"session_id": "live", "owner": "owner"}))
    rec = store.get("suspended")
    rec.id = "live"
    rec.status = "idle"
    rec.sandbox_id = handle.id
    rec.sandbox_root = str(handle.root)
    rec.sandbox_tags = handle.tags
    store.put(rec)
    with pytest.raises(SessionConflict, match="concurrency_limit"):
        plane.recover_session("suspended")
    assert checkpoints.calls == 0


def test_lease_decay_cannot_release_recovery_before_reservation_is_published(env):
    app, _backend, store, scheduler, _checkpoints = env
    state = app.state.v1_state
    state.set_lease("suspended", scheduler.acquire(provider="codex", account="acct"))
    original_put = store.put

    def decay_before_publish(rec):
        if rec.status == "creating":
            assert state.reconcile_leases(store, interval_s=0) == 0
            with pytest.raises(ScheduleRefused, match="account_busy"):
                scheduler.acquire(provider="codex", account="acct")
        original_put(rec)

    store.put = decay_before_publish
    app.state.plane.recover_session("suspended")
    assert scheduler.active_count == 1
    assert not state.recovering_leases
    assert store.get("suspended").status == "idle"
