"""SOR-82/A4 acceptance: idempotent create.

A client that loses the ``POST /v1/agents`` response must be able to retry
without allocating a second agent/run/worker. The suite sends BOTH candidate
contract keys — the ``Idempotency-Key`` header and a ``client_request_id``
body field — so the assertions hold whichever one the frozen contract keeps.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor


def _assert_single_agent_run_worker(env, agent_id: str, run_id: str) -> None:
    assert len(env.backend.handles) == 1, "retry allocated a second worker"
    agents = env.list_agents().json()["agents"]
    assert [a["id"] for a in agents] == [agent_id]
    runs = env.list_runs(agent_id).json()["runs"]
    assert [r["id"] for r in runs] == [run_id]


def test_retry_after_lost_response_returns_same_agent(recovery_env) -> None:
    env = recovery_env
    key = "idem-lost-response"
    first = env.post_agent(idempotency_key=key)
    assert first.status_code in (200, 201), first.text
    agent_id = first.json()["agent"]["id"]
    run_id = first.json()["run"]["id"]

    # The client never saw that response; it retries with the same key.
    retry = env.post_agent(idempotency_key=key)
    assert retry.status_code in (200, 201), retry.text
    assert retry.json()["agent"]["id"] == agent_id
    assert retry.json()["run"]["id"] == run_id
    _assert_single_agent_run_worker(env, agent_id, run_id)


def test_retry_after_run_finished_returns_same_agent(recovery_env) -> None:
    env = recovery_env
    key = "idem-after-finish"
    first = env.post_agent(idempotency_key=key)
    assert first.status_code in (200, 201), first.text
    agent_id = first.json()["agent"]["id"]
    run_id = first.json()["run"]["id"]
    env.wait_run(agent_id, run_id)

    retry = env.post_agent(idempotency_key=key)
    assert retry.status_code in (200, 201), retry.text
    assert retry.json()["agent"]["id"] == agent_id
    assert retry.json()["run"]["id"] == run_id
    _assert_single_agent_run_worker(env, agent_id, run_id)


def test_concurrent_duplicate_create_yields_one_worker(recovery_env) -> None:
    env = recovery_env
    key = "idem-concurrent"
    with ThreadPoolExecutor(max_workers=3) as pool:
        resps = list(pool.map(lambda _: env.post_agent(idempotency_key=key), range(3)))

    codes = {r.status_code for r in resps}
    assert codes <= {200, 201, 409}, f"unexpected statuses: {codes}"
    ok = [r for r in resps if r.status_code in (200, 201)]
    assert ok, "no create succeeded"
    agent_ids = {r.json()["agent"]["id"] for r in ok}
    assert len(agent_ids) == 1, f"duplicate create produced agents: {agent_ids}"
    agent_id = next(iter(agent_ids))
    assert len(env.backend.handles) == 1, "concurrent duplicates allocated extra workers"
    assert [a["id"] for a in env.list_agents().json()["agents"]] == [agent_id]


def test_retry_while_first_create_in_flight(recovery_env) -> None:
    env = recovery_env
    key = "idem-in-flight"
    gate = threading.Event()
    env.backend.create_gate = gate

    first: dict = {}
    retry: dict = {}
    t1 = threading.Thread(
        target=lambda: first.setdefault("resp", env.post_agent(idempotency_key=key)),
        daemon=True,
        name="create-1",
    )
    t2 = threading.Thread(
        target=lambda: retry.setdefault("resp", env.post_agent(idempotency_key=key)),
        daemon=True,
        name="create-2",
    )
    t1.start()
    try:
        assert env.backend.create_started.wait(timeout=10), "first create never started"
        # The first response is "lost": the client retries while the worker
        # is still being allocated.
        t2.start()
        t2.join(timeout=5)
    finally:
        gate.set()
        t1.join(timeout=15)
        t2.join(timeout=15)

    responses = [d["resp"] for d in (first, retry) if "resp" in d]
    assert responses, "no create request completed"
    for resp in responses:
        assert resp.status_code in (200, 201, 409), resp.text
    ok = [r for r in responses if r.status_code in (200, 201)]
    agent_ids = {r.json()["agent"]["id"] for r in ok}
    assert len(agent_ids) == 1, f"in-flight retry produced agents: {agent_ids}"
    assert len(env.backend.handles) == 1, "in-flight retry allocated a second worker"
    assert len(env.list_agents().json()["agents"]) == 1


def test_different_keys_create_different_agents(recovery_env) -> None:
    """Sanity check on the suite itself: distinct keys must not dedup."""
    env = recovery_env
    one = env.post_agent(idempotency_key="idem-a")
    two = env.post_agent(idempotency_key="idem-b")
    assert one.status_code == 201 and two.status_code == 201
    assert one.json()["agent"]["id"] != two.json()["agent"]["id"]
    # Create may ACK the durable creating record before allocation ends.
    until = time.monotonic() + 5
    while len(env.backend.handles) < 2 and time.monotonic() < until:
        time.sleep(0.01)
    assert len(env.backend.handles) == 2
    assert {h.tags["session_id"] for h in env.backend.handles} == {
        one.json()["agent"]["id"],
        two.json()["agent"]["id"],
    }


def test_retry_reuses_key_across_restart(recovery_env) -> None:
    """A retry landing after a control-plane restart still dedups."""
    env = recovery_env
    key = "idem-across-restart"
    first = env.post_agent(idempotency_key=key)
    assert first.status_code in (200, 201), first.text
    agent_id = first.json()["agent"]["id"]
    run_id = first.json()["run"]["id"]

    env2 = env.restart()
    time.sleep(0)  # restart boundary
    retry = env2.post_agent(idempotency_key=key)
    assert retry.status_code in (200, 201), retry.text
    assert retry.json()["agent"]["id"] == agent_id
    assert retry.json()["run"]["id"] == run_id
    assert len(env.backend.handles) == 1
