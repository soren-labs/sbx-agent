"""SOR-224: task/run execution queue, durable idempotency, status aggregation.

Follow-up runs default to a durable ``QUEUED`` run instead of the old
``turn_in_progress`` 409; ``on_busy=reject`` keeps the refusal. Task status
is an aggregate over runs + the workspace delivery record — a required PR
delivery failure can never read as ``finished`` — with a durable
``transitions`` log. ``cancel``/``retry``/``retry_after`` are
machine-readable.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.unit.api_v1.conftest import V1Env, create_agent, wait_run

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git binary unavailable")


def _slow_turn(monkeypatch: pytest.MonkeyPatch, seconds: str = "2") -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", seconds)


@pytest.fixture
def git_repo(tmp_path: Path) -> tuple[str, str]:
    if GIT is None:
        pytest.skip("git binary unavailable")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([GIT, "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("hi")
    subprocess.run([GIT, "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [GIT, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    sha = subprocess.run(
        [GIT, "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return f"file://{repo}", sha


@pytest.fixture
def credentialed(v1_env: V1Env) -> V1Env:
    v1_env.registry.put_credential_blob(
        "acct-codex-1",
        {"provider": "codex", "files": {".codex/auth.json": "{}"}},
    )
    return v1_env


def _task_body(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"prompt": {"text": "Create hello.txt."}}
    body.update(extra)
    return body


def _wait_task(client: TestClient, auth: dict, task_id: str, want: set[str]) -> dict:
    deadline = time.monotonic() + 15.0
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        task = client.get(f"/v1/tasks/{task_id}", headers=auth).json()["task"]
        last = task
        if task["status"] in want:
            return task
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} stuck at {last.get('status')}; want {want}")


def _create_task(client: TestClient, auth: dict, **extra: Any) -> dict:
    resp = client.post("/v1/tasks", json=_task_body(**extra), headers=auth)
    assert resp.status_code == 201, resp.text
    return resp.json()["task"]


# ---------------------------------------------------------------------------
# durable QUEUED — the new default for follow-up runs
# ---------------------------------------------------------------------------


def test_follow_up_run_defaults_to_durable_queue(
    client: TestClient, auth: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slow_turn(monkeypatch)
    agent = create_agent(client, auth)["agent"]
    resp = client.post(
        f"/v1/agents/{agent['id']}/runs",
        json={"prompt": {"text": "follow-up one"}},
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "QUEUED"
    third = client.post(
        f"/v1/agents/{agent['id']}/runs",
        json={"prompt": {"text": "follow-up two"}},
        headers=auth,
    )
    assert third.status_code == 201
    runs = client.get(f"/v1/agents/{agent['id']}/runs", headers=auth).json()["runs"]
    assert [r["id"] for r in runs] == ["run-1", "run-2", "run-3"]
    assert runs[1]["status"] == "QUEUED"
    assert runs[2]["status"] == "QUEUED"
    # FIFO drain: run-2 executes only after run-1 finishes.
    assert wait_run(client, auth, agent["id"], "run-1")["status"] == "FINISHED"
    assert wait_run(client, auth, agent["id"], "run-2")["status"] == "FINISHED"
    assert wait_run(client, auth, agent["id"], "run-3")["status"] == "FINISHED"


def test_cancel_queued_run_is_durable_and_skipped(
    client: TestClient, auth: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slow_turn(monkeypatch)
    agent = create_agent(client, auth)["agent"]
    resp = client.post(
        f"/v1/agents/{agent['id']}/runs",
        json={"prompt": {"text": "drop me"}},
        headers=auth,
    )
    assert resp.status_code == 201
    cancel = client.post(f"/v1/agents/{agent['id']}/runs/run-2/cancel", headers=auth)
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "CANCELLED"
    assert wait_run(client, auth, agent["id"], "run-1")["status"] == "FINISHED"
    # The cancelled queue head never dispatches; its terminal verdict holds.
    time.sleep(0.5)
    run2 = client.get(f"/v1/agents/{agent['id']}/runs/run-2", headers=auth).json()
    assert run2["status"] == "CANCELLED"


def test_run_create_idempotency_replay_and_conflict(
    client: TestClient, auth: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slow_turn(monkeypatch)
    agent = create_agent(client, auth)["agent"]
    headers = {**auth, "Idempotency-Key": "run-idem-1"}
    first = client.post(
        f"/v1/agents/{agent['id']}/runs",
        json={"prompt": {"text": "queued once"}},
        headers=headers,
    )
    assert first.status_code == 201, first.text
    replay = client.post(
        f"/v1/agents/{agent['id']}/runs",
        json={"prompt": {"text": "queued once"}},
        headers=headers,
    )
    assert replay.status_code in (200, 201)
    assert replay.json()["id"] == first.json()["id"]
    conflict = client.post(
        f"/v1/agents/{agent['id']}/runs",
        json={"prompt": {"text": "different body"}},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    runs = client.get(f"/v1/agents/{agent['id']}/runs", headers=auth).json()["runs"]
    assert [r["id"] for r in runs] == ["run-1", "run-2"]
    client.post(f"/v1/agents/{agent['id']}/runs/run-1/cancel", headers=auth)


# ---------------------------------------------------------------------------
# task-level queue / status aggregation / cancel / retry
# ---------------------------------------------------------------------------


@needs_git
def test_task_follow_up_queue_and_transitions(
    client: TestClient, auth: dict, credentialed: V1Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slow_turn(monkeypatch)
    task = _create_task(client, auth)
    tid, agent_id = task["id"], task["agent_id"]
    resp = client.post(
        f"/v1/tasks/{tid}/runs",
        json={"prompt": {"text": "follow-up"}},
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["run"]["status"] == "QUEUED"
    listing = client.get(f"/v1/tasks/{tid}/runs", headers=auth).json()
    assert [r["id"] for r in listing["runs"]] == ["run-1", "run-2"]
    assert listing["runs"][1]["queue_position"] == 1
    assert wait_run(client, auth, agent_id, "run-1")["status"] == "FINISHED"
    assert wait_run(client, auth, agent_id, "run-2")["status"] == "FINISHED"
    detail = client.get(f"/v1/tasks/{tid}", headers=auth).json()
    assert detail["task"]["status"] == "finished"
    # Durable machine-readable transition log on the task record.
    statuses = [t["status"] for t in detail["task"]["transitions"]]
    assert statuses == ["finished"]


@needs_git
def test_task_cancel_is_idempotent_and_marks_terminal(
    client: TestClient, auth: dict, credentialed: V1Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slow_turn(monkeypatch, "30")
    task = _create_task(client, auth)
    tid = task["id"]
    client.post(f"/v1/tasks/{tid}/runs", json={"prompt": {"text": "q"}}, headers=auth)
    resp = client.post(f"/v1/tasks/{tid}/cancel", headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "cancelled"
    again = client.post(f"/v1/tasks/{tid}/cancel", headers=auth)
    assert again.json()["task"]["status"] == "cancelled"
    detail = client.get(f"/v1/tasks/{tid}", headers=auth).json()
    assert detail["task"]["status"] == "cancelled"
    runs = client.get(f"/v1/tasks/{tid}/runs", headers=auth).json()["runs"]
    assert all(r["status"] in ("CANCELLED", "FINISHED") for r in runs)


@needs_git
def test_task_retry_after_run_error(
    client: TestClient, auth: dict, credentialed: V1Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "nonzero")
    task = _create_task(client, auth)
    tid = task["id"]
    _wait_task(client, auth, tid, {"error"})
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
    retry = client.post(f"/v1/tasks/{tid}/retry", json={}, headers=auth)
    assert retry.status_code == 200, retry.text
    assert retry.json()["run"]["id"] == "run-2"
    assert wait_run(client, auth, task["agent_id"], "run-2")["status"] == "FINISHED"


@needs_git
def test_task_active_retry_refuses_with_hint(
    client: TestClient, auth: dict, credentialed: V1Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slow_turn(monkeypatch, "30")
    task = _create_task(client, auth)
    resp = client.post(f"/v1/tasks/{task['id']}/retry", json={}, headers=auth)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "task_active"
    assert resp.json()["error"]["retry_after"] is not None
    client.post(f"/v1/tasks/{task['id']}/cancel", headers=auth)


@needs_git
def test_required_delivery_failure_blocks_finished(
    client: TestClient, auth: dict, credentialed: V1Env, git_repo
) -> None:
    """A FINISHED run whose required PR delivery failed → delivery_failed."""
    url, _ = git_repo
    task = _create_task(
        client,
        auth,
        source={"repo": url},
        delivery={"branch": "feat/x", "pull_request": {"title": "Add hello"}},
    )
    tid = task["id"]
    # file:// push succeeds; the PR step fails closed (not a github remote)
    # — the task must not read ``finished``.
    task = _wait_task(client, auth, tid, {"delivery_failed", "finished"})
    assert task["status"] == "delivery_failed"
    detail = client.get(f"/v1/tasks/{tid}", headers=auth).json()["task"]
    assert detail["delivery"]["required"] is True
    assert detail["delivery"]["status"] == "failed"
    assert detail["delivery"]["error"]
    retry = client.post(f"/v1/tasks/{tid}/retry", json={}, headers=auth)
    assert retry.status_code == 409  # publish fails again — deterministic
    again = _wait_task(client, auth, tid, {"delivery_failed"})
    assert again["status"] == "delivery_failed"


@needs_git
def test_delivery_satisfied_reports_finished(
    client: TestClient, auth: dict, credentialed: V1Env, git_repo
) -> None:
    """auto_publish on a pushable file:// remote → delivery delivered."""
    url, _ = git_repo
    task = _create_task(
        client,
        auth,
        source={"repo": url},
        delivery={"branch": "feat/y", "auto_publish": True},
    )
    tid = task["id"]
    task = _wait_task(client, auth, tid, {"finished", "delivery_failed", "error"})
    assert task["status"] == "finished"
    detail = client.get(f"/v1/tasks/{tid}", headers=auth).json()["task"]
    assert detail["delivery"]["status"] == "delivered"
    assert detail["delivery"]["pushed_head_sha"]
    assert detail["revision"]["status"] == "unchanged"
