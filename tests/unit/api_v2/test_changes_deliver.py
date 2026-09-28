"""``GET /v2/sessions/{id}/changes`` + ``POST .../deliver``: the changes facade.

Mirrors the SOR-225 lifecycle but through Session vocabulary — a
code-changing follow-up materializes a revision, ``changes`` reports the
diff state, and ``deliver`` pushes the durable payload to the remote.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.unit.api_v1.conftest import V1Env
from tests.unit.api_v2.conftest import wait_session

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git binary unavailable")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "sbx-test",
    "GIT_AUTHOR_EMAIL": "sbx-test@localhost",
    "GIT_COMMITTER_NAME": "sbx-test",
    "GIT_COMMITTER_EMAIL": "sbx-test@localhost",
}


def host_git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def make_origin(root: Path) -> tuple[Path, str]:
    repo = root / "origin"
    repo.mkdir()
    host_git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    host_git(repo, "add", "-A")
    host_git(repo, "commit", "-qm", "A")
    return repo, host_git(repo, "rev-parse", "HEAD")


def workdir(env: V1Env, agent_id: str) -> Path:
    rec = env.store.get(agent_id)
    assert rec is not None and rec.handle() is not None
    return Path(rec.handle().root) / "repo"


def commit_in_agent(env: V1Env, agent_id: str, name: str, content: str) -> str:
    wd = workdir(env, agent_id)
    (wd / name).write_text(content, encoding="utf-8")
    host_git(wd, "add", "-A")
    host_git(wd, "commit", "-qm", f"add {name}")
    return host_git(wd, "rev-parse", "HEAD")


def wait_idle(env: V1Env, agent_id: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = env.store.get(agent_id)
        if rec is not None and rec.status == "idle":
            return
        time.sleep(0.05)
    raise AssertionError(f"agent {agent_id} did not reach idle")


@pytest.fixture
def origin(tmp_path: Path) -> tuple[Path, str]:
    if GIT is None:
        pytest.skip("git binary unavailable")
    return make_origin(tmp_path)


def _agent_id(env: V1Env, session_id: str) -> str:
    record = env.app.state.task_store.get(session_id)
    assert record is not None and record.agent_id
    return record.agent_id


def _make_session(
    client: TestClient, auth: dict[str, str], origin: tuple[Path, str]
) -> dict[str, Any]:
    repo, _ = origin
    resp = client.post(
        "/v2/sessions",
        json={
            "prompt": "Create hello.txt.",
            "repository": {"repo": f"file://{repo}"},
        },
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    session = resp.json()["session"]
    wait_session(client, auth, session["id"], "finished")
    return session


@needs_git
class TestChangesAndDeliver:
    def test_changes_empty_session(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        session = _make_session(client, auth, origin)
        wait_idle(credentialed, _agent_id(credentialed, session["id"]))
        body = client.get(f"/v2/sessions/{session['id']}/changes", headers=auth)
        assert body.status_code == 200, body.text
        data = body.json()
        # An unchanged run materializes nothing.
        assert data["changes"]["status"] in ("none", "unchanged")
        assert data["changes"]["base_sha"] == origin[1]
        assert data["revisions"] == []

    def test_code_change_materializes_revision(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        session = _make_session(client, auth, origin)
        agent_id = _agent_id(credentialed, session["id"])
        wait_idle(credentialed, agent_id)
        head = commit_in_agent(credentialed, agent_id, "b.txt", "two\n")
        resp = client.post(
            f"/v2/sessions/{session['id']}/messages",
            json={"prompt": "more work"},
            headers=auth,
        )
        assert resp.status_code == 202, resp.text
        wait_session(client, auth, session["id"], "finished")
        data = client.get(f"/v2/sessions/{session['id']}/changes", headers=auth).json()
        assert data["changes"]["status"] == "ready"
        assert data["changes"]["head_sha"] == head
        assert data["changes"]["base_sha"] == origin[1]
        assert len(data["revisions"]) == 1
        rev = data["revisions"][0]
        # Revisions are addressed by sequence n — internal rev-/agent/task
        # ids and artifact refs stay off the wire.
        assert rev["n"] == 1
        assert rev["head_sha"] == head
        for key in ("id", "agent_id", "task_id", "run_id", "artifact_id"):
            assert key not in rev

    def test_deliver_pushes_to_remote(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        repo, base = origin
        session = _make_session(client, auth, origin)
        agent_id = _agent_id(credentialed, session["id"])
        wait_idle(credentialed, agent_id)
        head = commit_in_agent(credentialed, agent_id, "b.txt", "two\n")
        client.post(
            f"/v2/sessions/{session['id']}/messages",
            json={"prompt": "more work"},
            headers=auth,
        )
        wait_session(client, auth, session["id"], "finished")
        resp = client.post(
            f"/v2/sessions/{session['id']}/deliver",
            json={"branch": "session/work"},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        delivery = data["revision"]["delivery"]
        assert delivery["status"] == "delivered"
        assert delivery["branch"] == "session/work"
        # The durable push landed on the remote.
        assert host_git(repo, "rev-parse", "session/work") == head
        # And the changes view reports it.
        changes = client.get(f"/v2/sessions/{session['id']}/changes", headers=auth).json()[
            "changes"
        ]
        assert changes["pull_request"] is None or changes["status"] == "ready"

    def test_deliver_nothing_to_deliver(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        session = _make_session(client, auth, origin)
        wait_idle(credentialed, _agent_id(credentialed, session["id"]))
        resp = client.post(f"/v2/sessions/{session['id']}/deliver", json={}, headers=auth)
        assert resp.status_code in (404, 409)
        assert resp.json()["error"]["code"] in (
            "revision_not_found",
            "delivery_not_found",
            "revision_not_ready",
        )

    def test_deliver_failure_is_explicit(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        """PR delivery against a non-GitHub remote fails as an explicit,
        durable error — never a silent 200."""
        session = _make_session(client, auth, origin)
        agent_id = _agent_id(credentialed, session["id"])
        wait_idle(credentialed, agent_id)
        commit_in_agent(credentialed, agent_id, "b.txt", "two\n")
        client.post(
            f"/v2/sessions/{session['id']}/messages",
            json={"prompt": "more work"},
            headers=auth,
        )
        wait_session(client, auth, session["id"], "finished")
        resp = client.post(
            f"/v2/sessions/{session['id']}/deliver",
            json={"pull_request": {"title": "ship it"}},
            headers=auth,
        )
        assert resp.status_code >= 400
        rev = client.get(f"/v2/sessions/{session['id']}/changes", headers=auth).json()["revisions"][
            0
        ]
        assert rev["delivery"]["status"] == "failed"
