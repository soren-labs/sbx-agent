"""SOR-224 integration: durable QUEUED across restart, transport-failure
settlement, FIFO drain.

``QUEUED`` is a durable ledger state: the queued run's message and ledger
record are persisted before dispatch, so a control-plane restart cannot
lose parked work and a dispatch failure settles it as an explicit
``ERROR`` — never a perpetual QUEUED.
"""

from __future__ import annotations

import threading

import pytest


@pytest.fixture(autouse=True)
def _providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBX_PROVIDERS", "codex")


def _credentialed(env) -> None:
    env.registry.put_credential_blob(
        "acct-codex-1", {"provider": "codex", "files": {".codex/auth.json": "{}"}}
    )


def _post_run(env, agent_id: str, text: str) -> dict:
    resp = env.client.post(
        f"/v1/agents/{agent_id}/runs", json={"prompt": {"text": text}}, headers=env.auth
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_queued_run_durable_across_restart(recovery_env) -> None:
    env = recovery_env
    _credentialed(env)
    gate = threading.Event()
    env.backend.create_gate = gate
    try:
        created = env.post_agent()
        agent_id = created.json()["agent"]["id"]
        # run-1 still provisioning → the follow-up lands in durable QUEUED.
        queued = _post_run(env, agent_id, "queued follow-up")
        assert queued["status"] == "QUEUED"

        env2 = env.restart()
        # The QUEUED record + message survive in the durable ledger.
        restored = env2.get_run(agent_id, "run-2")
        assert restored.status_code == 200
        assert restored.json()["status"] == "QUEUED"
    finally:
        gate.set()
    assert env2.wait_run(agent_id, "run-1")["status"] == "FINISHED"
    assert env2.wait_run(agent_id, "run-2")["status"] == "FINISHED"


def test_queued_runs_drain_fifo(recovery_env, monkeypatch: pytest.MonkeyPatch) -> None:
    env = recovery_env
    _credentialed(env)
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "1")
    created = env.post_agent()
    agent_id = created.json()["agent"]["id"]
    second = _post_run(env, agent_id, "second")
    third = _post_run(env, agent_id, "third")
    assert second["status"] == "QUEUED"
    assert third["status"] == "QUEUED"
    env.wait_run(agent_id, "run-1")
    env.wait_run(agent_id, "run-2")
    env.wait_run(agent_id, "run-3")
    rec = env.store.get(agent_id)
    turns = [m.get("turn_id") for m in rec.messages if m.get("role") == "user"]
    assert turns == ["turn-1", "turn-2", "turn-3"]


def test_queued_run_transport_failure_settles_error(recovery_env) -> None:
    env = recovery_env
    _credentialed(env)
    gate = threading.Event()
    env.backend.create_gate = gate
    env.backend.exec_fail_for = {"turn"}
    try:
        created = env.post_agent()
        agent_id = created.json()["agent"]["id"]
        queued = _post_run(env, agent_id, "queued behind a failing dispatch")
        assert queued["status"] == "QUEUED"
    finally:
        gate.set()
    # run-1's dispatch fails on the injected transport error; the queued
    # run-2 hits the same fault and settles ERROR rather than wedging.
    run1 = env.wait_run(agent_id, "run-1", want=frozenset({"ERROR"}))
    assert run1["status"] == "ERROR"
    run2 = env.wait_run(agent_id, "run-2", want=frozenset({"ERROR"}))
    assert run2["status"] == "ERROR"
    assert run2["error"]["code"] == "runtime_error"


def test_run_idempotent_replay_across_restart(recovery_env) -> None:
    env = recovery_env
    _credentialed(env)
    gate = threading.Event()
    env.backend.create_gate = gate
    try:
        created = env.post_agent()
        agent_id = created.json()["agent"]["id"]
        headers = {**env.auth, "Idempotency-Key": "run-restart-1"}
        first = env.client.post(
            f"/v1/agents/{agent_id}/runs",
            json={"prompt": {"text": "dedup me"}},
            headers=headers,
        )
        assert first.status_code == 201, first.text
        assert first.json()["status"] == "QUEUED"
        env2 = env.restart()
        replay = env2.client.post(
            f"/v1/agents/{agent_id}/runs",
            json={"prompt": {"text": "dedup me"}},
            headers=headers,
        )
        assert replay.status_code in (200, 201)
        assert replay.json()["id"] == first.json()["id"]
        # No duplicate turn was allocated.
        runs = env2.list_runs(agent_id).json()["runs"]
        assert [r["id"] for r in runs] == ["run-1", "run-2"]
    finally:
        gate.set()
