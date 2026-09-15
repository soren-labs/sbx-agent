"""SOR-84: workflow metadata survives a control-plane restart.

The durable workflow index + run ledger let a fresh control-plane process
answer ``GET /v1/workflows/{id}`` and ``DELETE /v1/workflows/{id}`` from
nothing but the API key + ``workflow_id`` — no in-memory state required.
"""

from __future__ import annotations

from typing import Any


def _post_agent(env: Any, metadata: dict[str, str]) -> dict[str, Any]:
    resp = env.client.post(
        "/v1/agents",
        json={
            "prompt": {"text": "Create hello.txt in the workspace."},
            "agent": {"provider": "codex"},
            "metadata": metadata,
        },
        headers=env.auth,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_workflow_survives_control_plane_restart(make_recovery_env) -> None:
    env = make_recovery_env()
    a1 = _post_agent(env, {"workflow_id": "wf-r", "task_id": "impl", "role": "worker"})
    a2 = _post_agent(env, {"workflow_id": "wf-r", "task_id": "review", "role": "reviewer"})
    keep = _post_agent(env, {"workflow_id": "wf-keep", "task_id": "t", "role": "worker"})
    for agent in (a1, a2, keep):
        env.wait_run(agent["agent"]["id"])

    env2 = env.restart()

    resp = env2.client.get("/v1/workflows/wf-r", headers=env2.auth)
    assert resp.status_code == 200, resp.text
    view = resp.json()
    by_id = {a["agent_id"]: a for a in view["agents"]}
    assert set(by_id) == {a1["agent"]["id"], a2["agent"]["id"]}
    assert by_id[a1["agent"]["id"]]["task_id"] == "impl"
    assert by_id[a1["agent"]["id"]]["latest_run"]["status"] == "FINISHED"
    assert view["progress"]["all_terminal"] is True

    # Scoped cleanup on the fresh control plane touches only wf-r.
    resp = env2.client.delete("/v1/workflows/wf-r", headers=env2.auth)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert sorted(result["closed"]) == sorted([a1["agent"]["id"], a2["agent"]["id"]])
    assert env2.store.get(keep["agent"]["id"]).status != "closed"

    view2 = env2.client.get("/v1/workflows/wf-r", headers=env2.auth).json()
    assert view2["progress"]["open_agents"] == 0
