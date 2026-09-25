"""SOR-178 acceptance: auto-publish on run success + review-gated merge.

``git.auto_publish`` makes a FINISHED run execute the declared publish
(push + create-or-update PR) with no explicit ``/git/publish`` call —
that endpoint stays available for the manual path. ``git.merge`` allows
``/git/merge``, which refuses without an independent exact-sha review pin
and fails closed on any head drift. GitHub REST stays a monkeypatched
seam.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.unit.api_v1.conftest import V1Env, create_agent, wait_run

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "sbx-test",
    "GIT_AUTHOR_EMAIL": "sbx-test@localhost",
    "GIT_COMMITTER_NAME": "sbx-test",
    "GIT_COMMITTER_EMAIL": "sbx-test@localhost",
}

SHA_0 = "0" * 40


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


def workspace_decl(origin: Path, base: str) -> dict[str, str]:
    return {"repo": str(origin), "base_ref": "main", "base_sha": base}


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


def wait_workspace(
    v1_env: V1Env, agent_id: str, *, expect_pr: bool = False, timeout: float = 15.0
) -> dict[str, Any]:
    """Poll the workspace record — the auto-publish hook runs on the
    watcher thread just after the run's terminal verdict persists. The
    hook persists ``pushed_head_sha`` before the PR step, so an
    ``expect_pr`` wait must also hold for ``pull_request``."""
    deadline = time.monotonic() + timeout
    ws = None
    while time.monotonic() < deadline:
        rec = v1_env.app.state.workspaces.get(agent_id)
        if rec is not None:
            ws = rec
            if ws.publish_error is not None:
                break
            if ws.pushed_head_sha is not None and (not expect_pr or ws.pull_request is not None):
                break
        time.sleep(0.05)
    assert ws is not None
    return {
        "pushed_head_sha": ws.pushed_head_sha,
        "pull_request": ws.pull_request,
        "publish_error": ws.publish_error,
        "merge": ws.merge,
        "reviewed_head_sha": ws.reviewed_head_sha,
    }


def get_workspace(client: TestClient, auth: dict[str, str], agent_id: str) -> dict[str, Any]:
    resp = client.get(f"/v1/agents/{agent_id}/workspace", headers=auth)
    assert resp.status_code == 200, resp.text
    return resp.json()["workspace"]


@pytest.fixture
def origin(tmp_path: Path) -> tuple[Path, str]:
    return make_origin(tmp_path)


class TestPolicyValidation:
    def test_auto_publish_requires_push(
        self, client: TestClient, auth: dict[str, str], origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "x"},
                "agent": {"provider": "codex"},
                "workspace": workspace_decl(repo, base),
                "git": {"auto_publish": True},
            },
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "workspace_invalid"

    def test_merge_requires_auto_create_pr(
        self, client: TestClient, auth: dict[str, str], origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "x"},
                "agent": {"provider": "codex"},
                "workspace": workspace_decl(repo, base),
                "git": {"push": True, "merge": True},
            },
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "workspace_invalid"


class TestAutoPublish:
    def test_successful_run_publishes(
        self,
        client: TestClient,
        auth: dict[str, str],
        v1_env: V1Env,
        origin: tuple[Path, str],
    ) -> None:
        """git.auto_publish: a FINISHED run pushes the work branch itself —
        no explicit /git/publish call."""
        repo, base = origin
        agent = create_agent(
            client,
            auth,
            workspace=workspace_decl(repo, base),
            git={"branch": "sbx/work", "push": True, "auto_publish": True},
        )["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "FINISHED"
        ws = wait_workspace(v1_env, agent["id"])
        assert ws["pushed_head_sha"] == base  # workdir still at base
        assert host_git(repo, "rev-parse", "refs/heads/sbx/work") == base

        # A follow-up run's own commits are published when it finishes.
        wait_idle(v1_env, agent["id"])
        head = commit_in_agent(v1_env, agent["id"], "b.txt", "two\n")
        resp = client.post(
            f"/v1/agents/{agent['id']}/runs",
            json={"prompt": {"text": "more"}},
            headers=auth,
        )
        assert resp.status_code == 201, resp.text
        run = wait_run(client, auth, agent["id"], "run-2")
        assert run["status"] == "FINISHED"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            ws = wait_workspace(v1_env, agent["id"])
            if ws["pushed_head_sha"] == head:
                break
            time.sleep(0.05)
        assert ws["pushed_head_sha"] == head
        assert host_git(repo, "rev-parse", "refs/heads/sbx/work") == head

    def test_auto_publish_records_pull_request(
        self,
        client: TestClient,
        auth: dict[str, str],
        v1_env: V1Env,
        origin: tuple[Path, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "control.workspace.create_pull_request",
            lambda *a, **kw: {"number": 7, "html_url": "https://gh.test/pr/7", "state": "open"},
        )
        repo, base = origin
        agent = create_agent(
            client,
            auth,
            workspace=workspace_decl(repo, base),
            git={
                "branch": "sbx/work",
                "push": True,
                "auto_create_pr": True,
                "auto_publish": True,
            },
        )["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "FINISHED"
        ws = wait_workspace(v1_env, agent["id"], expect_pr=True)
        assert ws["pushed_head_sha"] == base
        assert ws["pull_request"]["number"] == 7
        assert ws["pull_request"]["head_sha"] == base

    def test_auto_publish_failure_is_nonfatal_but_recorded(
        self,
        client: TestClient,
        auth: dict[str, str],
        v1_env: V1Env,
        origin: tuple[Path, str],
    ) -> None:
        """A failed auto-publish never rewrites the FINISHED verdict — it
        lands as publish_error on the workspace record instead."""
        repo, base = origin
        agent = create_agent(
            client,
            auth,
            workspace=workspace_decl(repo, base),
            git={
                "branch": "sbx/work",
                "push": True,
                "auto_create_pr": True,  # no bridge → PR step fails
                "auto_publish": True,
            },
        )["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "FINISHED"
        ws = wait_workspace(v1_env, agent["id"])
        assert ws["pushed_head_sha"] == base  # push half still landed
        assert ws["publish_error"] is not None
        assert ws["publish_error"].startswith("repo_unavailable:")

    def test_no_auto_publish_means_no_push(
        self,
        client: TestClient,
        auth: dict[str, str],
        v1_env: V1Env,
        origin: tuple[Path, str],
    ) -> None:
        repo, base = origin
        agent = create_agent(
            client,
            auth,
            workspace=workspace_decl(repo, base),
            git={"branch": "sbx/work", "push": True},
        )["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "FINISHED"
        time.sleep(0.3)
        ws = get_workspace(client, auth, agent["id"])
        assert ws["pushed_head_sha"] is None


class TestMergeRoute:
    def _published_pr(
        self,
        client: TestClient,
        auth: dict[str, str],
        v1_env: V1Env,
        origin: tuple[Path, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[Path, dict[str, Any], str]:
        monkeypatch.setattr(
            "control.workspace.create_pull_request",
            lambda *a, **kw: {"number": 7, "html_url": "https://gh.test/pr/7", "state": "open"},
        )
        repo, base = origin
        agent = create_agent(
            client,
            auth,
            workspace=workspace_decl(repo, base),
            git={
                "branch": "sbx/work",
                "push": True,
                "auto_create_pr": True,
                "merge": True,
            },
        )["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "FINISHED"
        wait_idle(v1_env, agent["id"])
        head = commit_in_agent(v1_env, agent["id"], "b.txt", "two\n")
        resp = client.post(f"/v1/agents/{agent['id']}/git/publish", headers=auth)
        assert resp.status_code == 200, resp.text
        host_git(repo, "update-ref", "refs/pull/7/head", head)
        return repo, agent, head

    def test_merge_flow(
        self,
        client: TestClient,
        auth: dict[str, str],
        v1_env: V1Env,
        origin: tuple[Path, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo, agent, head = self._published_pr(client, auth, v1_env, origin, monkeypatch)

        # No independent review pin → review_required.
        resp = client.post(f"/v1/agents/{agent['id']}/git/merge", headers=auth)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "review_required"

        # Pin the exact head via the review surface.
        resp = client.post(
            f"/v1/agents/{agent['id']}/workspace/review",
            json={"head_sha": head},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text

        merges: list[dict[str, Any]] = []

        def fake_merge(backend: Any, h: Any, repo_url: str, **kwargs: Any) -> dict[str, Any]:
            merges.append(kwargs)
            return {"merged": True, "sha": SHA_0, "message": "merged"}

        monkeypatch.setattr("control.workspace.merge_pull_request", fake_merge)
        resp = client.post(f"/v1/agents/{agent['id']}/git/merge", headers=auth)
        assert resp.status_code == 200, resp.text
        ws = resp.json()["workspace"]
        assert merges[0]["sha"] == head
        assert ws["merge"]["merged"] is True
        assert ws["merge"]["merge_commit_sha"] == SHA_0
        assert ws["merge"]["head_sha"] == head
        assert ws["pull_request"]["state"] == "merged"

    def test_head_drift_requires_rereview(
        self,
        client: TestClient,
        auth: dict[str, str],
        v1_env: V1Env,
        origin: tuple[Path, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo, agent, head = self._published_pr(client, auth, v1_env, origin, monkeypatch)
        resp = client.post(
            f"/v1/agents/{agent['id']}/workspace/review",
            json={"head_sha": head},
            headers=auth,
        )
        assert resp.status_code == 200

        # The branch head moved past the pin → merge fails closed.
        head2 = commit_in_agent(v1_env, agent["id"], "c.txt", "three\n")
        resp = client.post(f"/v1/agents/{agent['id']}/git/publish", headers=auth)
        assert resp.status_code == 200
        host_git(repo, "update-ref", "refs/pull/7/head", head2)

        called: list[Any] = []
        monkeypatch.setattr(
            "control.workspace.merge_pull_request",
            lambda *a, **kw: called.append(1) or {"merged": True},
        )
        resp = client.post(f"/v1/agents/{agent['id']}/git/merge", headers=auth)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "head_sha_mismatch"
        assert not called

        # Re-review the new head → merge goes through.
        resp = client.post(
            f"/v1/agents/{agent['id']}/workspace/review",
            json={"head_sha": head2},
            headers=auth,
        )
        assert resp.status_code == 200
        resp = client.post(f"/v1/agents/{agent['id']}/git/merge", headers=auth)
        assert resp.status_code == 200, resp.text
        assert resp.json()["workspace"]["merge"]["head_sha"] == head2

    def test_merge_unknown_agent(self, client: TestClient, auth: dict[str, str]) -> None:
        resp = client.post("/v1/agents/agent-nope/git/merge", headers=auth)
        assert resp.status_code == 404
