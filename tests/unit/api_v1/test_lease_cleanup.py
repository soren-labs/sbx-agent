"""Lease lifecycle on ``/v1`` agent failure paths (SOR-80, SOR-82 A2).

A scheduler slot is held for the lifetime of its session: it must be freed
idempotently whenever ``POST /v1/agents`` fails after session creation, when
the background provisioner fails, and whenever ``DELETE /v1/agents/{id}``
runs — even when ``plane.close`` / provisioning / ``registry.touch`` raise
errors outside the handled exception types, and without masking the original
route error.
"""

from __future__ import annotations

from typing import Any

import pytest
from control.api_v1.state import V1State
from control.devin_pool import DevinAccountPool
from control.service import SessionConflict
from tests.unit.api_v1.conftest import create_agent, seed_account, wait_run

_DEVIN_AGENT = {"provider": "devin"}


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


def _v1_state(v1_env) -> V1State:
    state = getattr(v1_env.app.state, "v1_state", None)
    assert state is not None, "v1_state should exist after a /v1 request"
    return state


def _post_agent(client: Any, auth: dict[str, str]) -> Any:
    return client.post(
        "/v1/agents",
        json={"prompt": {"text": "hi"}, "agent": dict(_DEVIN_AGENT)},
        headers=auth,
    )


class TestCreateCleanup:
    def test_provision_failure_releases_lease_and_errors_run(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        """SOR-82 A2: ``backend.create`` fails in the worker → run-1 persists
        ERROR, the session goes terminal ``lost``, and the lease is freed."""
        pool = _devin_pool(v1_env)

        def boom(spec):
            raise RuntimeError("exec exploded")

        monkeypatch.setattr(v1_env.backend, "create", boom)
        resp = _post_agent(client, auth)
        assert resp.status_code == 201, resp.text
        agent_id = resp.json()["agent"]["id"]

        run = wait_run(client, auth, agent_id, "run-1")
        assert run["status"] == "ERROR"
        assert pool.active_count == 0
        (rec,) = [r for r in v1_env.store.list_all()]
        assert rec.status == "lost"
        assert rec.id not in _v1_state(v1_env).leases

    def test_dispatch_failure_marks_error_keeps_live_session(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        """Turn-1 exec failure after a healthy provision: run-1 is ERROR and
        the still-live session keeps its slot until delete."""
        pool = _devin_pool(v1_env)
        plane = v1_env.app.state.plane

        def conflict(session_id: str) -> str:
            raise SessionConflict("turn_in_progress")

        monkeypatch.setattr(plane, "post_queued_first_turn", conflict)
        resp = _post_agent(client, auth)
        assert resp.status_code == 201, resp.text
        agent_id = resp.json()["agent"]["id"]

        run = wait_run(client, auth, agent_id, "run-1")
        assert run["status"] == "ERROR"
        assert pool.active_count == 1  # live session still holds its slot
        assert client.delete(f"/v1/agents/{agent_id}", headers=auth).status_code == 200
        assert pool.active_count == 0

    def test_route_failure_after_open_releases_lease(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        """A failure between ``open_session`` and worker launch closes the
        half-created agent and frees the slot without masking the error."""
        pool = _devin_pool(v1_env)
        state_error = RuntimeError("run state store down")

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise state_error

        # Force v1_state to exist, then break its run-state store.
        _post_agent_ok = create_agent(client, auth, agent=dict(_DEVIN_AGENT))
        first_id = _post_agent_ok["agent"]["id"]
        assert pool.active_count == 1
        state = _v1_state(v1_env)
        monkeypatch.setattr(state.run_states, "begin", boom)

        with pytest.raises(RuntimeError, match="run state store down"):
            _post_agent(client, auth)

        assert pool.active_count == 1  # only the healthy first agent remains
        recs = {r.id: r for r in v1_env.store.list_all()}
        failed = next(r for r in recs.values() if r.id != first_id)
        assert failed.status == "closed"
        assert failed.id not in state.leases
        client.delete(f"/v1/agents/{first_id}", headers=auth)

    def test_cleanup_survives_close_failure_without_masking(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        pool = _devin_pool(v1_env)
        plane = v1_env.app.state.plane

        def close_boom(session_id: str) -> Any:
            raise RuntimeError("terminate exploded")

        calls = 0
        orig_touch = v1_env.registry.touch

        def flaky_touch(account_id: str, used_at: str) -> None:
            nonlocal calls
            calls += 1
            if calls > 1:  # first call is inside pool.acquire()
                raise RuntimeError("registry down")
            return orig_touch(account_id, used_at)

        monkeypatch.setattr(v1_env.registry, "touch", flaky_touch)
        monkeypatch.setattr(plane, "close", close_boom)
        # The route error survives; the close failure is not masked *over*
        # it and the slot is still freed.
        with pytest.raises(RuntimeError, match="registry down"):
            _post_agent(client, auth)
        assert pool.active_count == 0

    def test_registry_touch_failure_cleans_up(self, client, auth, v1_env, monkeypatch) -> None:
        pool = _devin_pool(v1_env)
        calls = 0
        orig_touch = v1_env.registry.touch

        def flaky_touch(account_id: str, used_at: str) -> None:
            nonlocal calls
            calls += 1
            if calls > 1:  # first call is inside pool.acquire()
                raise RuntimeError("registry down")
            return orig_touch(account_id, used_at)

        monkeypatch.setattr(v1_env.registry, "touch", flaky_touch)
        with pytest.raises(RuntimeError, match="registry down"):
            _post_agent(client, auth)

        assert pool.active_count == 0
        (rec,) = v1_env.store.list_all()
        assert rec.status == "closed"
        assert rec.id not in _v1_state(v1_env).leases

    def test_create_session_failure_releases_lease(self, client, auth, v1_env) -> None:
        pool = _devin_pool(v1_env)
        v1_env.app.state.plane.max_concurrent = 1
        first = create_agent(client, auth, agent=dict(_DEVIN_AGENT))["agent"]["id"]
        assert pool.active_count == 1

        resp = _post_agent(client, auth)
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "concurrency_limit"
        # Only the surviving session keeps its slot.
        assert pool.active_count == 1
        client.delete(f"/v1/agents/{first}", headers=auth)


class TestDeleteCleanup:
    def test_delete_releases_lease_when_close_fails(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        pool = _devin_pool(v1_env)
        agent_id = create_agent(client, auth, agent=dict(_DEVIN_AGENT))["agent"]["id"]
        assert pool.active_count == 1

        def close_boom(session_id: str) -> Any:
            raise RuntimeError("terminate exploded")

        monkeypatch.setattr(v1_env.app.state.plane, "close", close_boom)
        with pytest.raises(RuntimeError, match="terminate exploded"):
            client.delete(f"/v1/agents/{agent_id}", headers=auth)
        assert pool.active_count == 0
        assert agent_id not in _v1_state(v1_env).leases

    def test_delete_missing_agent_releases_stale_lease(self, client, auth, v1_env) -> None:
        pool = _devin_pool(v1_env)
        state = V1State()
        v1_env.app.state.v1_state = state
        lease = pool.acquire(provider="devin", account="acct-devin-1")
        state.set_lease("ghost-agent", lease)
        assert pool.active_count == 1

        resp = client.delete("/v1/agents/ghost-agent", headers=auth)
        assert resp.status_code == 404
        assert pool.active_count == 0
        assert "ghost-agent" not in state.leases

    def test_double_delete_is_idempotent(self, client, auth, v1_env) -> None:
        pool = _devin_pool(v1_env)
        agent_id = create_agent(client, auth, agent=dict(_DEVIN_AGENT))["agent"]["id"]
        assert pool.active_count == 1

        assert client.delete(f"/v1/agents/{agent_id}", headers=auth).status_code == 200
        assert pool.active_count == 0
        # Second delete: no lease left to release, no error.
        assert client.delete(f"/v1/agents/{agent_id}", headers=auth).status_code == 200
        assert pool.active_count == 0
