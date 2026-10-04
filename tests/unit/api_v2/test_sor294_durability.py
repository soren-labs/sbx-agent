"""REV-008: a follow-up ACK refers to durable intent, including late failure."""

import threading
import time

from control.api_v1.state import V1State
from control.api_v2 import routes
from control.backend import SandboxPoll
from tests.unit.api_v2.conftest import create_session, wait_session


def settled_agent(client, auth, app):
    sid = create_session(client, auth)["session"]["id"]
    wait_session(client, auth, sid, "finished")
    agent = app.state.task_store.get(sid).agent_id
    until = time.monotonic() + 10
    while app.state.plane.get(agent).status != "idle" and time.monotonic() < until:
        time.sleep(0.02)
    return sid, agent


def test_rev008_late_dead_compute_failure_keeps_prompt_run_and_replay(
    client,
    v1_env,
    auth,
    monkeypatch,
):
    app = v1_env.app
    sid, agent = settled_agent(client, auth, app)
    gate = threading.Event()
    app.state.v2_ack_budget_s = 0
    original = v1_env.backend.exec

    def failed_exec(handle, argv, env=None):
        if "turn" in argv:
            assert gate.wait(10)
            raise RuntimeError("compute disappeared")
        return original(handle, argv, env=env)

    monkeypatch.setattr(v1_env.backend, "exec", failed_exec)
    monkeypatch.setattr(v1_env.backend, "poll", lambda _: SandboxPoll(False, 0))
    headers = {**auth, "Idempotency-Key": "durable-followup"}
    try:
        response = client.post(
            f"/v2/sessions/{sid}/messages",
            headers=headers,
            json={"prompt": "Keep this acknowledged prompt"},
        )
        assert response.status_code == 202, response.text
        assert response.json()["message"]["n"] == 2
        assert app.state.run_store.get(agent, 2) is not None
        assert app.state.plane.get(agent).messages[-1]["text"] == "Keep this acknowledged prompt"
    finally:
        gate.set()
    until = time.monotonic() + 10
    while time.monotonic() < until:
        run = app.state.run_store.get(agent, 2)
        if run.terminal:
            break
        time.sleep(0.02)
    assert run.status == "ERROR" and run.error["retryable"]
    history = client.get(f"/v2/sessions/{sid}/history", headers=auth).json()
    assert history["runs"][-1]["n"] == 2 and history["runs"][-1]["status"] == "failed"
    app.state.v1_state = V1State(run_states=app.state.run_states)
    replay = client.post(
        f"/v2/sessions/{sid}/messages",
        headers=headers,
        json={"prompt": "Keep this acknowledged prompt"},
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["message"]["n"] == 2
    assert replay.json()["message"]["status"] == "failed"
    conflict = client.post(
        f"/v2/sessions/{sid}/messages", headers=headers, json={"prompt": "Different prompt"}
    )
    assert (
        conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
    )
    assert len(app.state.run_store.list(agent)) == 2


def test_rev008_accepted_queue_survives_missing_worker_and_cancel(
    client,
    v1_env,
    auth,
    monkeypatch,
):
    app = v1_env.app
    budget = routes._run_with_budget
    sid, agent = settled_agent(client, auth, app)
    # Simulate a service death after persistence but before dispatch begins.
    monkeypatch.setattr(routes, "_run_with_budget", lambda *a: (False, {}))
    response = client.post(
        f"/v2/sessions/{sid}/messages", headers=auth, json={"prompt": "Survive restart"}
    )
    assert response.status_code == 202
    assert app.state.run_store.get(agent, 2).status == "QUEUED"
    app.state.plane.reconcile_turns()
    until = time.monotonic() + 10
    while not app.state.run_store.get(agent, 2).terminal and time.monotonic() < until:
        time.sleep(0.02)
    assert app.state.run_store.get(agent, 2).status == "FINISHED"
    until = time.monotonic() + 10
    while app.state.plane.get(agent).status != "idle" and time.monotonic() < until:
        time.sleep(0.02)
    response = client.post(
        f"/v2/sessions/{sid}/messages", headers=auth, json={"prompt": "Cancel retained intent"}
    )
    assert response.status_code == 202
    # A reconstructed process has no request-local mutation leases.
    with routes._MUTATION_LOCK:
        routes._MUTATIONS_INFLIGHT.pop(sid, None)
    monkeypatch.setattr(routes, "_run_with_budget", budget)
    assert client.post(f"/v2/sessions/{sid}/cancel", headers=auth).status_code == 200
    until = time.monotonic() + 5
    while not app.state.run_store.get(agent, 3).terminal and time.monotonic() < until:
        time.sleep(0.02)
    assert app.state.run_store.get(agent, 3).status == "CANCELLED"
    assert any(
        m.get("text") == "Cancel retained intent" for m in app.state.plane.get(agent).messages
    )
