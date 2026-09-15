"""SOR-63/D2: the scheduler lease outlives the run — terminal/cancel/reaper.

A lease belongs to the *session*, not the run: cancel and terminal run
states keep it held while the sandbox lives; it is freed on close/delete,
by the worker when the session dies mid-provision, and by the reaper when
a session goes ``lost``/``timed_out`` underneath — via the same
``release_lease_for_action`` wiring ``control/modal_app.py``'s cron uses.
Every release is idempotent.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from control.devin_pool import DevinAccountPool
from control.reaper import reap
from control.scheduler import AccountScheduler
from control.service import release_lease_for_action
from tests.unit.api_v1.conftest import create_agent, seed_account, wait_run, wait_sandbox

_DEVIN = {"provider": "devin"}
_BASIC = ("sbx", "sbx")


def _devin_pool(v1_env) -> DevinAccountPool:
    seed_account(
        v1_env,
        "acct-devin-1",
        provider="devin",
        max_concurrent=8,
        models=("swe-2-high",),
        secret_name="sbx-acct-devin-1",
    )
    pool = DevinAccountPool(v1_env.registry, account_id="acct-devin-1")
    v1_env.app.state.scheduler = pool
    return pool


def _codex_scheduler(v1_env) -> AccountScheduler:
    """Two codex accounts behind the real AccountScheduler (SOR-63/D1)."""
    for account_id in ("acct-pool-a", "acct-pool-b"):
        seed_account(v1_env, account_id, provider="codex", models=("gpt-5.6-luna",))
    scheduler = AccountScheduler(v1_env.registry)
    v1_env.app.state.scheduler = scheduler
    return scheduler


def _v1_state(v1_env) -> Any:
    state = getattr(v1_env.app.state, "v1_state", None)
    assert state is not None
    return state


def _gate_backend_create(v1_env, monkeypatch) -> tuple[threading.Event, threading.Event]:
    entered = threading.Event()
    release = threading.Event()
    orig = v1_env.backend.create

    def gated(spec):
        entered.set()
        assert release.wait(timeout=30), "test never released backend.create"
        return orig(spec)

    monkeypatch.setattr(v1_env.backend, "create", gated)
    return entered, release


def _reap(v1_env, now: datetime) -> None:
    """Run the reaper exactly as ``modal_app.reap_cron`` wires it."""
    v1_state = getattr(v1_env.app.state, "v1_state", None)
    reap(
        v1_env.store,
        v1_env.backend,
        now,
        on_action=lambda action: release_lease_for_action(v1_state, action),
    )


class TestCancelKeepsLease:
    def test_cancel_queued_run_keeps_lease_until_close(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        """Cancel marks run-1 CANCELLED but the slot is session-scoped: the
        still-live agent keeps it until delete."""
        pool = _devin_pool(v1_env)
        entered, release = _gate_backend_create(v1_env, monkeypatch)
        agent = create_agent(client, auth, agent=dict(_DEVIN))["agent"]
        assert entered.wait(timeout=5)
        assert pool.active_count == 1

        cancelled = client.post(f"/v1/agents/{agent['id']}/runs/run-1/cancel", headers=auth)
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"

        release.set()
        rec = wait_sandbox(v1_env, agent["id"])
        assert rec.status == "idle"  # provisioned; the cancelled turn never ran
        assert pool.active_count == 1  # the session still owns its slot

        deleted = client.delete(f"/v1/agents/{agent['id']}", headers=auth)
        assert deleted.status_code == 200
        assert pool.active_count == 0
        assert agent["id"] not in _v1_state(v1_env).leases


class TestReaperReleasesLease:
    def test_dead_sandbox_reaped_terminal_releases(self, client, auth, v1_env) -> None:
        """Idle session whose sandbox died out-of-band → timed_out + free."""
        pool = _devin_pool(v1_env)
        agent = create_agent(client, auth, agent=dict(_DEVIN))["agent"]
        wait_run(client, auth, agent["id"], "run-1")  # settles to idle
        rec = v1_env.store.get(agent["id"])
        assert rec.status == "idle"
        assert pool.active_count == 1

        # Sandbox dies behind the control plane's back (crash, Modal kill).
        handle = rec.handle()
        assert handle is not None
        v1_env.backend.terminate(handle)

        _reap(v1_env, datetime.now(UTC) + timedelta(seconds=5))
        rec = v1_env.store.get(agent["id"])
        assert rec.status == "timed_out"
        assert pool.active_count == 0
        assert agent["id"] not in _v1_state(v1_env).leases

    def test_idle_timeout_reaped_releases(self, client, auth, v1_env) -> None:
        pool = _devin_pool(v1_env)
        agent = create_agent(client, auth, agent=dict(_DEVIN))["agent"]
        wait_run(client, auth, agent["id"], "run-1")  # settles to idle
        rec = v1_env.store.get(agent["id"])
        assert rec.status == "idle"
        assert pool.active_count == 1

        # Idle past the session's timeout → terminate + timed_out + free.
        _reap(v1_env, rec.last_activity_at + timedelta(seconds=3600))
        rec = v1_env.store.get(agent["id"])
        assert rec.status == "timed_out"
        assert pool.active_count == 0

    def test_creating_past_grace_reaped_lost_releases(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        """A wedged provisioner (record ``creating`` past create grace) is
        marked ``lost`` by the reaper and its slot freed; the worker's late
        provision must not resurrect or re-hold it."""
        pool = _devin_pool(v1_env)
        entered, release = _gate_backend_create(v1_env, monkeypatch)
        agent = create_agent(client, auth, agent=dict(_DEVIN))["agent"]
        assert entered.wait(timeout=5)
        assert pool.active_count == 1
        try:
            rec = v1_env.store.get(agent["id"])
            assert rec.status == "creating"
            _reap(v1_env, rec.created_at + timedelta(seconds=400))
            rec = v1_env.store.get(agent["id"])
            assert rec.status == "lost"
            assert pool.active_count == 0
        finally:
            release.set()

        # The late worker sees the terminal record, tears the orphan sandbox
        # down, persists run-1 terminal, and re-releasing is a no-op.
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] in ("ERROR", "CANCELLED")
        assert pool.active_count == 0

    def test_second_reap_is_idempotent(self, client, auth, v1_env) -> None:
        pool = _devin_pool(v1_env)
        agent = create_agent(client, auth, agent=dict(_DEVIN))["agent"]
        wait_sandbox(v1_env, agent["id"])
        assert client.delete(f"/v1/agents/{agent['id']}", headers=auth).status_code == 200
        assert pool.active_count == 0
        # Reaping a terminal session must not double-decrement.
        _reap(v1_env, datetime.now(UTC) + timedelta(hours=1))
        _reap(v1_env, datetime.now(UTC) + timedelta(hours=2))
        assert pool.active_count == 0

    def test_pool_lease_released_by_reaper(self, client, auth, v1_env) -> None:
        """Same wiring through the multi-account AccountScheduler (D1)."""
        scheduler = _codex_scheduler(v1_env)
        agent = create_agent(client, auth)["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        rec = v1_env.store.get(agent["id"])
        assert scheduler.active_count == 1
        v1_env.backend.terminate(rec.handle())
        _reap(v1_env, datetime.now(UTC) + timedelta(seconds=5))
        assert v1_env.store.get(agent["id"]).status == "timed_out"
        assert scheduler.active_count == 0


class TestInternalApiClose:
    def test_internal_session_delete_releases_v1_lease(self, client, auth, v1_env) -> None:
        """``DELETE /api/sessions`` on a /v1-created agent frees the slot."""
        pool = _devin_pool(v1_env)
        agent = create_agent(client, auth, agent=dict(_DEVIN))["agent"]
        wait_sandbox(v1_env, agent["id"])
        assert pool.active_count == 1

        resp = client.delete(f"/api/sessions/{agent['id']}", auth=_BASIC)
        assert resp.status_code == 200
        assert pool.active_count == 0
        assert agent["id"] not in _v1_state(v1_env).leases
