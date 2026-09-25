"""SOR-222/223 integration: task records are durable across restarts.

The task store is a durable seam (file store locally, ``sbx-tasks`` Dict on
Modal): a task's ``request``/``resolved`` record, its idempotency pin, and
its derived status must survive a control-plane restart — the same
acceptance axis this suite holds ``/v1/agents`` to.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _providers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Task resolution gates on the deploy-selected provider set
    # (``SBX_PROVIDERS``); the suite's scrubbed env defaults to platform-only.
    monkeypatch.setenv("SBX_PROVIDERS", "codex")


def _credentialed(env) -> None:
    """The seeded codex account needs auth material to be eligible."""
    env.registry.put_credential_blob(
        "acct-codex-1", {"provider": "codex", "files": {".codex/auth.json": "{}"}}
    )


def _task_body() -> dict:
    return {"prompt": {"text": "Create hello.txt in the workspace."}}


def test_task_record_survives_restart(recovery_env) -> None:
    env = recovery_env
    _credentialed(env)
    created = env.client.post("/v1/tasks", json=_task_body(), headers=env.auth)
    assert created.status_code == 201, created.text
    task = created.json()["task"]
    assert task["request"]["prompt"]["text"].startswith("Create hello.txt")
    assert task["resolved"]["execution"]["account_id"] == "acct-codex-1"
    env.wait_run(task["agent_id"], task["run_id"])

    env2 = env.restart()
    detail = env2.client.get(f"/v1/tasks/{task['id']}", headers=env2.auth)
    assert detail.status_code == 200, detail.text
    after = detail.json()["task"]
    # The durable record carries the verbatim request and the resolved plan.
    assert after["request"] == task["request"]
    assert after["resolved"] == task["resolved"]
    assert after["agent_id"] == task["agent_id"]
    # Status derives live from the durable run — not a stored string.
    assert after["status"] == "finished"
    listing = env2.client.get("/v1/tasks", headers=env2.auth)
    assert [t["id"] for t in listing.json()["tasks"]] == [task["id"]]


def test_task_idempotent_replay_across_restart(recovery_env) -> None:
    env = recovery_env
    _credentialed(env)
    key = "task-idem-restart"
    headers = {**env.auth, "Idempotency-Key": key}
    first = env.client.post("/v1/tasks", json=_task_body(), headers=headers)
    assert first.status_code == 201, first.text
    task_id = first.json()["task"]["id"]
    agent_id = first.json()["agent"]["id"]

    # The in-memory idempotency entry is gone after restart; the durable
    # pin on the task record must still dedup the replay.
    env2 = env.restart()
    replay = env2.client.post("/v1/tasks", json=_task_body(), headers=headers)
    assert replay.status_code in (200, 201), replay.text
    assert replay.json()["task"]["id"] == task_id
    assert replay.json()["agent"]["id"] == agent_id
    assert len(env.backend.handles) == 1, "replay allocated a second worker"

    # Same key, different body → conflict, even across the restart boundary.
    conflict = env2.client.post(
        "/v1/tasks", json=_task_body() | {"name": "different"}, headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_task_preflight_leaves_no_durable_state(recovery_env) -> None:
    env = recovery_env
    _credentialed(env)
    resp = env.client.post("/v1/tasks/preflight", json=_task_body(), headers=env.auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    env2 = env.restart()
    assert env2.client.get("/v1/tasks", headers=env2.auth).json()["tasks"] == []
    assert env2.list_agents().json()["agents"] == []
