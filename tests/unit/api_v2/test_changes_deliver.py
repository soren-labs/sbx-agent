"""SOR-256: ``changes`` + ``deliver`` — Session-level facade over revisions.

Clients never see Revision/Agent/Task ids: ``GET changes`` reports the
session's workspace delta, ``POST deliver`` ships it under caller options.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.unit.api_v1.test_sor225 import commit_in_agent, make_origin, wait_idle
from tests.unit.api_v2.conftest import V1Env, create_session, wait_session

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git binary unavailable")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "sbx-test",
    "GIT_AUTHOR_EMAIL": "sbx-test@localhost",
    "GIT_COMMITTER_NAME": "sbx-test",
    "GIT_COMMITTER_EMAIL": "sbx-test@localhost",
}


@pytest.fixture
def origin(tmp_path: Path) -> tuple[Path, str]:
    if GIT is None:
        pytest.skip("git binary unavailable")
    return make_origin(tmp_path)


def _agent_id(env: V1Env, client: TestClient, auth: dict[str, str], session_id: str) -> str:
    """The internal agent id for a session — tests read the store, not the API."""
    record = env.app.state.task_store.get(session_id)
    assert record is not None and record.agent_id is not None
    return record.agent_id


def _session_with_changes(
    client: TestClient, auth: dict[str, str], env: V1Env, origin: tuple[Path, str]
) -> tuple[str, str]:
    """A finished session whose agent committed a change → ``(session_id, head)``."""
    repo, _base = origin
    session = create_session(client, auth, repository={"repo": f"file://{repo}"})
    wait_session(client, auth, session["id"], "finished")
    agent_id = _agent_id(env, client, auth, session["id"])
    wait_idle(env, agent_id)
    head = commit_in_agent(env, agent_id, "b.txt", "two\n")
    resp = client.post(
        f"/v2/sessions/{session['id']}/messages",
        json={"prompt": "commit it"},
        headers=auth,
    )
    assert resp.status_code == 202, resp.text
    wait_session(client, auth, session["id"], "finished")
    wait_idle(env, agent_id)
    return session["id"], head


@needs_git
def test_changes_reports_session_delta(
    client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
) -> None:
    session_id, head = _session_with_changes(client, auth, credentialed, origin)
    repo, base = origin
    resp = client.get(f"/v2/sessions/{session_id}/changes", headers=auth)
    assert resp.status_code == 200, resp.text
    changes = resp.json()["changes"]
    assert changes["status"] == "ready"
    assert changes["repo"].endswith("origin") or changes["repo"]
    assert changes["base_sha"] == base
    assert changes["head_sha"] == head
    assert changes["count"] == 1
    rev = changes["revisions"][0]
    assert rev["n"] == 1 and rev["status"] == "ready"
    assert rev["head_sha"] == head
    # Internal ids stay internal: no agent/run fields on the public row.
    assert "agent_id" not in changes
    assert "agent_id" not in rev


@needs_git
def test_changes_empty_when_no_repo(
    client: TestClient, auth: dict[str, str], credentialed: V1Env
) -> None:
    session = create_session(client, auth)
    wait_session(client, auth, session["id"], "finished")
    resp = client.get(f"/v2/sessions/{session['id']}/changes", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["changes"]["status"] in ("none", "unchanged")
    assert resp.json()["changes"]["revisions"] == []


@needs_git
def test_deliver_pushes_session_changes(
    client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
) -> None:
    session_id, head = _session_with_changes(client, auth, credentialed, origin)
    repo, _base = origin
    resp = client.post(
        f"/v2/sessions/{session_id}/deliver",
        json={"branch": "feat/session"},
        headers=auth,
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["accepted"] is True
    deadline = time.monotonic() + 15
    delivery = body["delivery"]
    while delivery is None or delivery["status"] == "pending":
        if time.monotonic() > deadline:
            raise AssertionError("delivery never settled")
        time.sleep(0.2)
        delivery = client.get(f"/v2/sessions/{session_id}", headers=auth).json()["session"][
            "delivery"
        ]
    assert delivery["status"] == "delivered"
    assert delivery["pushed_head_sha"] == head
    remote = subprocess.run(
        ["git", "ls-remote", str(repo), "refs/heads/feat/session"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    assert remote == head


@needs_git
def test_deliver_without_work_404(
    client: TestClient, auth: dict[str, str], credentialed: V1Env
) -> None:
    session = create_session(client, auth)
    wait_session(client, auth, session["id"], "finished")
    resp = client.post(f"/v2/sessions/{session['id']}/deliver", json={}, headers=auth)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "revision_not_found"
