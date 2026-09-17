"""SOR-128 acceptance: git policy on create, publish, PR-ref handoff, review.

Exercises the /v1 surface end-to-end with the local backend + real on-disk
git repos/file remotes. GitHub REST stays a monkeypatched seam — the shared
GitHub identity is never exercised and never faked: review is a comment.
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


def get_workspace(client: TestClient, auth: dict[str, str], agent_id: str) -> dict[str, Any]:
    resp = client.get(f"/v1/agents/{agent_id}/workspace", headers=auth)
    assert resp.status_code == 200, resp.text
    return resp.json()["workspace"]


@pytest.fixture
def origin(tmp_path: Path) -> tuple[Path, str]:
    return make_origin(tmp_path)


def _create_with_workspace(
    client: TestClient, auth: dict[str, str], decl: dict[str, str], **extra: Any
) -> dict[str, Any]:
    body = create_agent(client, auth, workspace=decl, **extra)
    agent = body["agent"]
    wait_run(client, auth, agent["id"], "run-1")
    return agent


def make_pr_ref(origin: Path, ref: str = "refs/pull/7/head", filename: str = "b.txt") -> str:
    """Commit on a side branch + pin it at a PR-style ref; return the head."""
    host_git(origin, "checkout", "-qb", "work")
    (origin / filename).write_text("two\n", encoding="utf-8")
    host_git(origin, "add", "-A")
    host_git(origin, "commit", "-qm", f"add {filename}")
    head = host_git(origin, "rev-parse", "HEAD")
    host_git(origin, "update-ref", ref, head)
    host_git(origin, "checkout", "-q", "main")
    return head


class TestGitPolicyValidation:
    def test_git_requires_workspace(self, client: TestClient, auth: dict[str, str]) -> None:
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "x"},
                "agent": {"provider": "codex"},
                "git": {"branch": "sbx/x", "push": True},
            },
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "workspace_invalid"

    def test_auto_create_pr_requires_push(
        self, client: TestClient, auth: dict[str, str], origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "x"},
                "agent": {"provider": "codex"},
                "workspace": workspace_decl(repo, base),
                "git": {"auto_create_pr": True},
            },
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "workspace_invalid"

    @pytest.mark.parametrize("branch", ["bad;rm", "a..b", "-x", "a b"])
    def test_unsafe_branch_rejected(
        self, client: TestClient, auth: dict[str, str], origin: tuple[Path, str], branch: str
    ) -> None:
        repo, base = origin
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "x"},
                "agent": {"provider": "codex"},
                "workspace": workspace_decl(repo, base),
                "git": {"branch": branch, "push": True},
            },
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "workspace_invalid"

    def test_unknown_git_key_rejected(
        self, client: TestClient, auth: dict[str, str], origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "x"},
                "agent": {"provider": "codex"},
                "workspace": workspace_decl(repo, base),
                "git": {"push": True, "bogus": 1},
            },
            headers=auth,
        )
        # extra="forbid" on the new models → request validation error,
        # which the v1 router maps to the canonical 400 body.
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_provider"


class TestCreateWithPolicy:
    def test_branch_materializes_and_persists(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        agent = _create_with_workspace(
            client,
            auth,
            workspace_decl(repo, base),
            git={"branch": "sbx/work", "push": True, "auto_create_pr": True},
        )
        wd = workdir(v1_env, agent["id"])
        assert host_git(wd, "rev-parse", "--abbrev-ref", "HEAD") == "sbx/work"
        ws = get_workspace(client, auth, agent["id"])
        assert ws["checkout_sha"] == ws["head_sha"] == base
        assert ws["branch"] == "sbx/work"
        assert ws["git"]["branch"] == "sbx/work"
        assert ws["git"]["push"] is True
        assert ws["git"]["auto_create_pr"] is True
        assert ws["git"]["target"] == "main"  # resolved from base_ref
        assert ws["pushed_head_sha"] is None
        assert ws["pull_request"] is None

    def test_default_branch(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        agent = _create_with_workspace(client, auth, workspace_decl(repo, base), git={"push": True})
        ws = get_workspace(client, auth, agent["id"])
        assert ws["branch"] == f"sbx/{agent['id']}"

    def test_no_git_policy_unchanged(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        agent = _create_with_workspace(client, auth, workspace_decl(repo, base))
        ws = get_workspace(client, auth, agent["id"])
        assert ws["git"] is None and ws["branch"] is None
        assert host_git(workdir(v1_env, agent["id"]), "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


class TestPublishRoute:
    def test_push_publishes_branch(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        agent = _create_with_workspace(
            client, auth, workspace_decl(repo, base), git={"branch": "sbx/work", "push": True}
        )
        wait_idle(v1_env, agent["id"])
        head = commit_in_agent(v1_env, agent["id"], "b.txt", "two\n")
        resp = client.post(f"/v1/agents/{agent['id']}/git/publish", headers=auth)
        assert resp.status_code == 200, resp.text
        ws = resp.json()["workspace"]
        assert ws["pushed_head_sha"] == head
        assert ws["pull_request"] is None
        # The remote really carries the branch at exactly the pushed head.
        assert host_git(repo, "rev-parse", "refs/heads/sbx/work") == head

    def test_publish_without_policy(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        agent = _create_with_workspace(client, auth, workspace_decl(repo, base))
        wait_idle(v1_env, agent["id"])
        resp = client.post(f"/v1/agents/{agent['id']}/git/publish", headers=auth)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "workspace_invalid"

    def test_publish_unknown_agent(self, client: TestClient, auth: dict[str, str]) -> None:
        resp = client.post("/v1/agents/agent-nope/git/publish", headers=auth)
        assert resp.status_code == 404

    def test_auto_create_pr_without_bridge(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        """auto_create_pr needs the opt-in GitHub bridge; without it the
        publish pushes then fails explicitly — never silently skips."""
        repo, base = origin
        agent = _create_with_workspace(
            client,
            auth,
            workspace_decl(repo, base),
            git={"branch": "sbx/work", "push": True, "auto_create_pr": True},
        )
        wait_idle(v1_env, agent["id"])
        commit_in_agent(v1_env, agent["id"], "b.txt", "two\n")
        resp = client.post(f"/v1/agents/{agent['id']}/git/publish", headers=auth)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "repo_unavailable"

    def test_auto_create_pr_records_metadata(
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
        agent = _create_with_workspace(
            client,
            auth,
            workspace_decl(repo, base),
            git={
                "branch": "sbx/work",
                "push": True,
                "auto_create_pr": True,
                "draft": True,
            },
        )
        wait_idle(v1_env, agent["id"])
        head = commit_in_agent(v1_env, agent["id"], "b.txt", "two\n")
        resp = client.post(f"/v1/agents/{agent['id']}/git/publish", headers=auth)
        assert resp.status_code == 200, resp.text
        pr = resp.json()["workspace"]["pull_request"]
        assert pr["number"] == 7
        assert pr["ref"] == "refs/pull/7/head"
        assert pr["head_sha"] == head
        assert pr["draft"] is True
        # Durable: a fresh GET shows the same structured metadata.
        ws = get_workspace(client, auth, agent["id"])
        assert ws["pull_request"]["ref"] == "refs/pull/7/head"
        assert ws["pushed_head_sha"] == head


class TestPullRequestHandoff:
    def test_create_from_pr_ref(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        head = make_pr_ref(repo)
        agent = _create_with_workspace(
            client,
            auth,
            workspace_decl(repo, base),
            handoff={"pull_request": {"ref": "refs/pull/7/head", "head_sha": head}},
        )
        wd = workdir(v1_env, agent["id"])
        assert host_git(wd, "rev-parse", "HEAD") == head
        assert (wd / "b.txt").read_text() == "two\n"
        ws = get_workspace(client, auth, agent["id"])
        assert ws["checkout_sha"] == ws["head_sha"] == head

    def test_drift_fails_closed(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        head = make_pr_ref(repo)
        # The ref moved after the pin was taken.
        host_git(repo, "update-ref", "refs/pull/7/head", base)
        agent = create_agent(
            client,
            auth,
            workspace=workspace_decl(repo, base),
            handoff={"pull_request": {"ref": "refs/pull/7/head", "head_sha": head}},
        )["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "ERROR"
        assert "head_sha_mismatch" in (run["error"] or {}).get("message", "")
        rec = v1_env.store.get(agent["id"])
        assert rec is not None and rec.status == "closed"

    def test_pr_ref_unsafe_rejected(
        self, client: TestClient, auth: dict[str, str], origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "x"},
                "agent": {"provider": "codex"},
                "workspace": workspace_decl(repo, base),
                "handoff": {"pull_request": {"ref": "bad;rm", "head_sha": SHA_0}},
            },
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "workspace_invalid"

    def test_pr_ref_requires_workspace(self, client: TestClient, auth: dict[str, str]) -> None:
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "x"},
                "agent": {"provider": "codex"},
                "handoff": {"pull_request": {"ref": "pull/7/head", "head_sha": SHA_0}},
            },
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "workspace_invalid"

    def test_exactly_one_of_still_enforced(
        self, client: TestClient, auth: dict[str, str], origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        for handoff in (
            {"pull_request": {"ref": "pull/7/head", "head_sha": SHA_0}, "head_sha": SHA_0},
            {"pull_request": {"ref": "pull/7/head", "head_sha": SHA_0}, "artifact_id": "a"},
            {},
        ):
            resp = client.post(
                "/v1/agents",
                json={
                    "prompt": {"text": "x"},
                    "agent": {"provider": "codex"},
                    "workspace": workspace_decl(repo, base),
                    "handoff": handoff,
                },
                headers=auth,
            )
            assert resp.status_code == 400
            assert resp.json()["error"]["code"] == "workspace_invalid"

    def test_live_handoff_from_pr_ref(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        head = make_pr_ref(repo)
        agent = _create_with_workspace(client, auth, workspace_decl(repo, base))
        wait_idle(v1_env, agent["id"])
        resp = client.post(
            f"/v1/agents/{agent['id']}/handoff",
            json={"pull_request": {"ref": "pull/7/head", "head_sha": head}},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["workspace"]["head_sha"] == head
        assert (workdir(v1_env, agent["id"]) / "b.txt").read_text() == "two\n"

    def test_live_handoff_drift_fails_closed(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        head = make_pr_ref(repo)
        agent = _create_with_workspace(client, auth, workspace_decl(repo, base))
        wait_idle(v1_env, agent["id"])
        host_git(repo, "update-ref", "refs/pull/7/head", base)  # drift
        resp = client.post(
            f"/v1/agents/{agent['id']}/handoff",
            json={"pull_request": {"ref": "pull/7/head", "head_sha": head}},
            headers=auth,
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "head_sha_mismatch"


class TestReviewComment:
    def _agent_with_pr(
        self,
        client: TestClient,
        auth: dict[str, str],
        v1_env: V1Env,
        origin: tuple[Path, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[dict[str, Any], str]:
        monkeypatch.setattr(
            "control.workspace.create_pull_request",
            lambda *a, **kw: {"number": 7, "html_url": "https://gh.test/pr/7"},
        )
        repo, base = origin
        agent = _create_with_workspace(
            client,
            auth,
            workspace_decl(repo, base),
            git={"branch": "sbx/work", "push": True, "auto_create_pr": True},
        )
        wait_idle(v1_env, agent["id"])
        head = commit_in_agent(v1_env, agent["id"], "b.txt", "two\n")
        resp = client.post(f"/v1/agents/{agent['id']}/git/publish", headers=auth)
        assert resp.status_code == 200, resp.text
        return agent, head

    def test_comment_posts_and_records(
        self,
        client: TestClient,
        auth: dict[str, str],
        v1_env: V1Env,
        origin: tuple[Path, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent, head = self._agent_with_pr(client, auth, v1_env, origin, monkeypatch)
        seen: list[dict[str, Any]] = []

        def fake_comment(backend: Any, handle: Any, repo: str, **kwargs: Any) -> dict[str, Any]:
            seen.append(kwargs)
            return {"html_url": "https://gh.test/pr/7#issuecomment-1"}

        monkeypatch.setattr("control.workspace.create_issue_comment", fake_comment)
        resp = client.post(
            f"/v1/agents/{agent['id']}/workspace/review",
            json={"head_sha": head, "comment": "review: lgtm (machine check)"},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        ws = resp.json()["workspace"]
        assert ws["reviewed_head_sha"] == head
        assert ws["pull_request"]["review_comment_url"] == "https://gh.test/pr/7#issuecomment-1"
        # A comment, never a formal review approval.
        assert seen[0]["number"] == 7
        assert "lgtm" in seen[0]["body"]

    def test_comment_without_pr_fails(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        agent = _create_with_workspace(client, auth, workspace_decl(repo, base), git={"push": True})
        wait_idle(v1_env, agent["id"])
        resp = client.post(
            f"/v1/agents/{agent['id']}/workspace/review",
            json={"comment": "review"},
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "workspace_invalid"

    def test_review_without_comment_unchanged(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        agent = _create_with_workspace(client, auth, workspace_decl(repo, base))
        wait_idle(v1_env, agent["id"])
        resp = client.post(
            f"/v1/agents/{agent['id']}/workspace/review",
            json={"head_sha": base},
            headers=auth,
        )
        assert resp.status_code == 200
        assert resp.json()["workspace"]["reviewed_head_sha"] == base
