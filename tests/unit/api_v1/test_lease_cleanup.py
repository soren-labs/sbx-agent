"""Lease lifecycle on ``/v1`` agent failure paths (SOR-80).

A scheduler slot is held for the lifetime of its session: it must be freed
idempotently whenever ``POST /v1/agents`` fails after session creation and
whenever ``DELETE /v1/agents/{id}`` runs — even when ``plane.close`` /
``plane.post_message`` / ``registry.touch`` raise errors outside the handled
exception types, and without masking the original route error.
"""

from __future__ import annotations

from typing import Any

import pytest
from control.api_v1.state import V1State
from control.devin_pool import DevinAccountPool
from control.service import SessionConflict
from tests.unit.api_v1.conftest import create_agent, seed_account

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
    def test_post_message_runtime_error_releases_lease_and_closes(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        pool = _devin_pool(v1_env)
        plane = v1_env.app.state.plane

        def boom(session_id: str, text: str) -> str:
            raise RuntimeError("exec exploded")

        monkeypatch.setattr(plane, "post_message", boom)
        with pytest.raises(RuntimeError, match="exec exploded"):
            _post_agent(client, auth)

        assert pool.active_count == 0
        (rec,) = [r for r in v1_env.store.list_all()]
        assert rec.status == "closed"
        assert rec.id not in _v1_state(v1_env).leases

    def test_post_message_conflict_returns_409_and_releases(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        pool = _devin_pool(v1_env)
        plane = v1_env.app.state.plane

        def conflict(session_id: str, text: str) -> str:
            raise SessionConflict("turn_in_progress")

        monkeypatch.setattr(plane, "post_message", conflict)
        resp = _post_agent(client, auth)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "turn_in_progress"

        assert pool.active_count == 0
        (rec,) = v1_env.store.list_all()
        assert rec.status == "closed"
        assert rec.id not in _v1_state(v1_env).leases

    def test_post_message_keyerror_returns_404_and_releases(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        pool = _devin_pool(v1_env)
        plane = v1_env.app.state.plane

        def missing(session_id: str, text: str) -> str:
            raise KeyError(session_id)

        monkeypatch.setattr(plane, "post_message", missing)
        resp = _post_agent(client, auth)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"
        assert pool.active_count == 0

    def test_cleanup_survives_close_failure_without_masking(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        pool = _devin_pool(v1_env)
        plane = v1_env.app.state.plane

        def conflict(session_id: str, text: str) -> str:
            raise SessionConflict("session_not_runnable")

        def close_boom(session_id: str) -> Any:
            raise RuntimeError("terminate exploded")

        monkeypatch.setattr(plane, "post_message", conflict)
        monkeypatch.setattr(plane, "close", close_boom)
        # The mapped route error survives; the close failure is not masked
        # *over* it and the slot is still freed.
        resp = _post_agent(client, auth)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "session_not_runnable"
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
