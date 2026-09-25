"""SOR-225: task-scoped Revision / Review / Delivery / Merge + handoff refs.

End-to-end over the public API: a code-changing run materializes a durable
revision, delivery operates on the revision (usable after teardown), review
is durable and goes stale on a newer revision, merge is review-gated, and
``task_id``/``pr_url`` handoffs need no caller-supplied ref/SHA.
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
from tests.unit.api_v1.conftest import V1Env, create_agent, wait_run

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


def workspace_decl(origin: Path, base: str, url: bool = False) -> dict[str, str]:
    repo = f"file://{origin}" if url else str(origin)
    return {"repo": repo, "base_ref": "main", "base_sha": base}


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
def credentialed(v1_env: V1Env) -> V1Env:
    v1_env.registry.put_credential_blob(
        "acct-codex-1",
        {"provider": "codex", "files": {".codex/auth.json": "{}"}},
    )
    return v1_env


@pytest.fixture
def origin(tmp_path: Path) -> tuple[Path, str]:
    if GIT is None:
        pytest.skip("git binary unavailable")
    return make_origin(tmp_path)


def _make_task(
    client: TestClient, auth: dict[str, str], origin: tuple[Path, str]
) -> dict[str, Any]:
    """A finished task whose agent has a prepared workspace."""
    repo, _ = origin
    resp = client.post(
        "/v1/tasks",
        json={
            "prompt": {"text": "Create hello.txt."},
            "source": {"repo": f"file://{repo}"},
        },
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    wait_run(client, auth, body["agent"]["id"], body["run"]["id"])
    return body


def _run_again(client: TestClient, auth: dict[str, str], agent_id: str, n: int = 2) -> None:
    resp = client.post(
        f"/v1/agents/{agent_id}/runs",
        json={"prompt": {"text": "do more work"}},
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    run = wait_run(client, auth, agent_id, f"run-{n}")
    assert run["status"] == "FINISHED"


@needs_git
class TestRevisionLifecycle:
    def test_code_changing_run_materializes_revision(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        repo, base = origin
        body = _make_task(client, auth, origin)
        task, agent = body["task"], body["agent"]
        # An unchanged run materializes nothing.
        wait_idle(credentialed, agent["id"])
        resp = client.get(f"/v1/tasks/{task['id']}/revisions", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["revisions"] == []
        # A code-changing follow-up run produces revision n=1.
        head = commit_in_agent(credentialed, agent["id"], "b.txt", "two\n")
        _run_again(client, auth, agent["id"])
        rows = client.get(f"/v1/tasks/{task['id']}/revisions", headers=auth).json()
        assert len(rows["revisions"]) == 1
        rev = rows["revisions"][0]
        assert rev["n"] == 1
        assert rev["status"] == "ready"
        assert rev["task_id"] == task["id"]
        assert rev["head_sha"] == head
        assert rev["base_sha"] == base
        assert rev["artifact_id"]
        # Same rows visible on the agent surface, and `latest`/`1` resolve.
        agent_rows = client.get(f"/v1/agents/{agent['id']}/revisions", headers=auth).json()[
            "revisions"
        ]
        assert [r["id"] for r in agent_rows] == [rev["id"]]
        for ref in ("latest", "1", rev["id"]):
            got = client.get(f"/v1/tasks/{task['id']}/revisions/{ref}", headers=auth)
            assert got.status_code == 200
            assert got.json()["revision"]["id"] == rev["id"]

    def test_revisions_require_task_ownership(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        body = _make_task(client, auth, origin)
        other_key, other_token = credentialed.keys.create(label="other", scopes=("agents",))
        other = {"Authorization": f"Bearer {other_token}"}
        resp = client.get(f"/v1/tasks/{body['task']['id']}/revisions", headers=other)
        assert resp.status_code == 404
        resp = client.post(f"/v1/tasks/{body['task']['id']}/deliver", json={}, headers=other)
        assert resp.status_code == 404


@needs_git
class TestDeliverReviewMerge:
    def test_deliver_pushes_after_review_gate(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        repo, _ = origin
        body = _make_task(client, auth, origin)
        task, agent = body["task"], body["agent"]
        wait_idle(credentialed, agent["id"])
        head = commit_in_agent(credentialed, agent["id"], "b.txt", "two\n")
        _run_again(client, auth, agent["id"])

        # Deliver the revision — branch lands on the remote.
        resp = client.post(
            f"/v1/tasks/{task['id']}/deliver",
            json={"branch": "feat/x"},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        delivery = resp.json()["revision"]["delivery"]
        assert delivery["status"] == "delivered"
        assert delivery["pushed_head_sha"] == head
        remote_sha = subprocess.run(
            ["git", "ls-remote", str(repo), "refs/heads/feat/x"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()[0]
        assert remote_sha == head

        # Merge without a delivered PR is explicit, not silent.
        merge = client.post(f"/v1/tasks/{task['id']}/merge", json={}, headers=auth)
        assert merge.status_code == 409
        assert merge.json()["error"]["code"] == "delivery_not_found"

        # A durable review records identity + verdict + pin.
        review = client.post(
            f"/v1/tasks/{task['id']}/reviews",
            json={
                "verdict": "approve",
                "reviewer": {"identity": "agent:reviewer-1", "agent_id": agent["id"]},
            },
            headers=auth,
        )
        assert review.status_code == 201, review.text
        row = review.json()["review"]
        assert row["verdict"] == "approve"
        assert row["reviewed_head_sha"] == head
        # reviewer_agent_id == revision's own agent → dependent.
        assert row["independent"] is False
        listed = client.get(f"/v1/tasks/{task['id']}/reviews", headers=auth)
        assert [r["id"] for r in listed.json()["reviews"]] == [row["id"]]

    def test_new_revision_stales_prior_review(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        body = _make_task(client, auth, origin)
        task, agent = body["task"], body["agent"]
        wait_idle(credentialed, agent["id"])
        commit_in_agent(credentialed, agent["id"], "b.txt", "two\n")
        _run_again(client, auth, agent["id"], n=2)
        client.post(
            f"/v1/tasks/{task['id']}/reviews",
            json={"verdict": "approve", "reviewer": {"identity": "key:reviewer"}},
            headers=auth,
        )
        # A second code-changing run materializes rev-2 → rev-1 review stales.
        wait_idle(credentialed, agent["id"])
        commit_in_agent(credentialed, agent["id"], "c.txt", "three\n")
        _run_again(client, auth, agent["id"], n=3)
        reviews = client.get(f"/v1/tasks/{task['id']}/reviews", headers=auth).json()
        assert reviews["reviews"][0]["stale"] is True
        revs = client.get(f"/v1/tasks/{task['id']}/revisions", headers=auth).json()
        assert [r["n"] for r in revs["revisions"]] == [1, 2]

    def test_deliver_failure_is_first_class(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        """Delivery failure is durable revision state + an explicit error."""
        body = _make_task(client, auth, origin)
        task, agent = body["task"], body["agent"]
        wait_idle(credentialed, agent["id"])
        commit_in_agent(credentialed, agent["id"], "b.txt", "two\n")
        _run_again(client, auth, agent["id"])
        # PR delivery against a non-github remote fails explicitly and is
        # persisted on the revision.
        resp = client.post(
            f"/v1/tasks/{task['id']}/deliver",
            json={"pull_request": {"title": "ship it"}},
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "workspace_invalid"
        rev = client.get(f"/v1/tasks/{task['id']}/revisions/latest", headers=auth).json()[
            "revision"
        ]
        assert rev["delivery"]["status"] == "failed"
        assert rev["delivery"]["error"]["code"] == "workspace_invalid"


@needs_git
class TestHandoffByTaskOrPr:
    def test_handoff_by_task_id_latest_revision(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        repo, base = origin
        body = _make_task(client, auth, origin)
        task, agent_a = body["task"], body["agent"]
        wait_idle(credentialed, agent_a["id"])
        head_a = commit_in_agent(credentialed, agent_a["id"], "b.txt", "two\n")
        _run_again(client, auth, agent_a["id"])

        # Consumer needs no artifact_id/ref/SHA — task_id + latest.
        agent_b = create_agent(
            client,
            auth,
            workspace=workspace_decl(repo, base, url=True),
            handoff={"task_id": task["id"]},
        )["agent"]
        wait_run(client, auth, agent_b["id"], "run-1")
        assert (workdir(credentialed, agent_b["id"]) / "b.txt").read_text() == "two\n"
        ws = client.get(f"/v1/agents/{agent_b['id']}/workspace", headers=auth).json()["workspace"]
        assert ws["head_sha"] == head_a

    def test_handoff_revision_requires_task_id(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        repo, base = origin
        body = create_agent(client, auth, workspace=workspace_decl(repo, base))
        agent = body["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        wait_idle(credentialed, agent["id"])
        resp = client.post(
            f"/v1/agents/{agent['id']}/handoff",
            json={"revision": "latest"},
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "workspace_invalid"

    def test_handoff_by_pr_url_resolves_server_side(
        self,
        client: TestClient,
        auth: dict[str, str],
        credentialed: V1Env,
        origin,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo, base = origin
        body = _make_task(client, auth, origin)
        agent_a = body["agent"]
        wait_idle(credentialed, agent_a["id"])
        head_a = commit_in_agent(credentialed, agent_a["id"], "b.txt", "two\n")
        # Publish the change under a PR-shaped ref on the shared remote.
        host_git(
            workdir(credentialed, agent_a["id"]),
            "push",
            "-q",
            str(repo),
            f"{head_a}:refs/pull/7/head",
        )
        # Server-side URL resolution is the seam under test — the caller
        # supplies only the URL.
        monkeypatch.setattr(
            credentialed.app.state.revisions,
            "resolve_pr_url",
            lambda url: ("refs/pull/7/head", head_a),
        )
        agent_b = create_agent(
            client,
            auth,
            workspace=workspace_decl(repo, base, url=True),
            handoff={"pr_url": "https://github.com/acme/widgets/pull/7"},
        )["agent"]
        wait_run(client, auth, agent_b["id"], "run-1")
        assert (workdir(credentialed, agent_b["id"]) / "b.txt").read_text() == "two\n"
