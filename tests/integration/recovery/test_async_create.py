"""SOR-82/A4 acceptance: async create.

``POST /v1/agents`` must return quickly with ``run.status == CREATING``
while worker startup continues in the background; CREATING and RUNNING must
be observable through plain ``GET``. A startup failure must land in durable
history as a structured ERROR, not a vanished request.
"""

from __future__ import annotations

import threading
import time

import pytest
from tests.integration.recovery.support import (
    TERMINAL_RUN_STATUSES,
    assert_structured_error,
)


def test_create_returns_while_sandbox_create_in_flight(recovery_env) -> None:
    env = recovery_env
    gate = threading.Event()
    env.backend.create_gate = gate

    result: dict = {}
    thread = threading.Thread(
        target=lambda: result.setdefault("resp", env.post_agent()),
        daemon=True,
        name="post-agents",
    )
    thread.start()
    try:
        assert env.backend.create_started.wait(timeout=10), "worker create never started"

        # The agent and its first run must be queryable while the worker is
        # still starting.
        agents = env.list_agents().json()["agents"]
        assert len(agents) == 1
        agent_id = agents[0]["id"]
        assert agents[0]["status"] == "creating"

        run = env.get_run(agent_id, "run-1")
        assert run.status_code == 200, run.text
        assert run.json()["status"] == "CREATING"

        # POST must have returned already — while create is still gated.
        thread.join(timeout=2.0)
        assert "resp" in result, "POST /v1/agents blocked on worker startup"
        resp = result["resp"]
        assert resp.status_code == 201, resp.text
        assert resp.json()["agent"]["id"] == agent_id
        assert resp.json()["run"]["status"] == "CREATING"
    finally:
        gate.set()
        thread.join(timeout=15)

    seen = env.observe_run_statuses(agent_id, "run-1", timeout=20)
    assert "CREATING" in seen
    assert "RUNNING" in seen
    assert seen[-1] in TERMINAL_RUN_STATUSES


def test_create_returns_before_worker_startup_finishes(recovery_env) -> None:
    env = recovery_env
    env.backend.create_delay_s = 2.0
    start = time.monotonic()
    resp = env.post_agent()
    elapsed = time.monotonic() - start
    assert resp.status_code == 201, resp.text
    assert elapsed < 1.0, f"POST /v1/agents blocked {elapsed:.2f}s on worker startup"
    body = resp.json()
    assert body["run"]["status"] == "CREATING"

    seen = env.observe_run_statuses(body["agent"]["id"], body["run"]["id"], timeout=20)
    assert "CREATING" in seen
    assert "RUNNING" in seen
    assert seen[-1] == "FINISHED"


@pytest.mark.parametrize("fault", ["create", "init"])
def test_failed_worker_startup_persists_structured_error(recovery_env, fault) -> None:
    env = recovery_env
    if fault == "create":
        env.backend.create_failures.append(RuntimeError("injected sandbox create failure"))
    else:
        env.backend.exec_fail_for.add("init")

    resp = env.post_agent()
    # Create is accepted with a CREATING run; the failure lands asynchronously.
    assert resp.status_code == 201, resp.text
    body = resp.json()
    agent_id, run_id = body["agent"]["id"], body["run"]["id"]

    run = env.wait_run(agent_id, run_id, timeout=15)
    assert run["status"] == "ERROR"
    err = assert_structured_error(run)
    assert err["code"] != "cancelled"

    # The failure is durable history: still queryable after a restart.
    env2 = env.restart()
    again = env2.get_run(agent_id, run_id)
    assert again.status_code == 200
    assert again.json() == run
