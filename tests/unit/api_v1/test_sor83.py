"""SOR-83 acceptance: workspace decl, artifact handoff, teardown durability.

A/B/C chain over the public API with the local backend + stub runner:
producer A snapshots its declared workspace into a durable artifact,
consumer B applies it by ``artifact_id``, consumer C pins the exact commit
by ``head_sha``. Source crosses agents through artifacts/commits only —
never through prompt text.
"""

from __future__ import annotations

import hashlib
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


class TestWorkspaceDeclaration:
    def test_create_prepares_declared_checkout(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        agent = _create_with_workspace(client, auth, workspace_decl(repo, base))
        rec = v1_env.store.get(agent["id"])
        assert host_git(workdir(v1_env, agent["id"]), "rev-parse", "HEAD") == base
        ws = client.get(f"/v1/agents/{agent['id']}/workspace", headers=auth)
        assert ws.status_code == 200, ws.text
        body = ws.json()["workspace"]
        assert body["checkout_sha"] == base
        assert body["head_sha"] == base
        assert body["reviewed_head_sha"] is None
        assert rec is not None and rec.status == "idle"

    def test_wrong_base_sha_fails_explicitly(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, _ = origin
        agent = create_agent(client, auth, workspace=workspace_decl(repo, SHA_0))["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "ERROR"
        assert "base_sha_mismatch" in (run["error"] or {}).get("message", "")
        # The agent is closed rather than left running on the wrong version.
        rec = v1_env.store.get(agent["id"])
        assert rec is not None and rec.status == "closed"

    def test_handoff_requires_workspace(self, client: TestClient, auth: dict[str, str]) -> None:
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "x"},
                "agent": {"provider": "codex"},
                "handoff": {"artifact_id": "art-nope"},
            },
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "workspace_invalid"

    def test_handoff_unknown_artifact(
        self, client: TestClient, auth: dict[str, str], origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "x"},
                "agent": {"provider": "codex"},
                "workspace": workspace_decl(repo, base),
                "handoff": {"artifact_id": "art-nope"},
            },
            headers=auth,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "artifact_not_found"


class TestArtifactLifecycle:
    def test_snapshot_list_detail_download_and_teardown(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        agent = _create_with_workspace(client, auth, workspace_decl(repo, base))
        head = commit_in_agent(v1_env, agent["id"], "b.txt", "two\n")

        resp = client.post(f"/v1/agents/{agent['id']}/artifacts", json={}, headers=auth)
        assert resp.status_code == 201, resp.text
        manifest = resp.json()["artifact"]
        artifact_id = manifest["artifact_id"]
        assert manifest["base_sha"] == base
        assert manifest["head_sha"] == head
        assert manifest["repo"] == str(repo)
        assert manifest["producer"]["agent_id"] == agent["id"]
        assert manifest["producer"]["run_id"] == "run-1"

        # Run-1's durable record carries the artifact ref.
        run = client.get(f"/v1/agents/{agent['id']}/runs/run-1", headers=auth).json()
        assert f"artifact://{artifact_id}" in run["artifact_refs"]

        # List + detail endpoints expose the manifest.
        listed = client.get(f"/v1/artifacts?agent_id={agent['id']}", headers=auth).json()[
            "artifacts"
        ]
        assert any(m["artifact_id"] == artifact_id for m in listed)
        detail = client.get(f"/v1/artifacts/{artifact_id}", headers=auth)
        assert detail.status_code == 200
        assert detail.json()["payloads"]["patch.diff"] == manifest["payloads"]["patch.diff"]

        # Sandbox teardown does not take the artifact with it.
        resp = client.delete(f"/v1/agents/{agent['id']}", headers=auth)
        assert resp.status_code == 200, resp.text
        download = client.get(
            f"/v1/artifacts/{artifact_id}/download?member=patch.diff", headers=auth
        )
        assert download.status_code == 200
        assert hashlib.sha256(download.content).hexdigest() == manifest["payloads"]["patch.diff"]
        member = client.get(
            f"/v1/artifacts/{artifact_id}/download?member=files/b.txt", headers=auth
        )
        assert member.status_code == 200
        assert member.content == b"two\n"

    def test_leak_gate_fails_closed(
        self,
        client: TestClient,
        auth: dict[str, str],
        v1_env: V1Env,
        origin: tuple[Path, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo, base = origin
        canary = "sk-canary-REDACTED-0000"
        monkeypatch.setenv("CODEX_AUTH_JSON", canary)
        agent = _create_with_workspace(client, auth, workspace_decl(repo, base))
        (workdir(v1_env, agent["id"]) / "leak.txt").write_text(
            f"token={canary}\n", encoding="utf-8"
        )
        resp = client.post(f"/v1/agents/{agent['id']}/artifacts", json={}, headers=auth)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "artifact_secret"
        # Nothing was persisted under a half-written artifact.
        assert client.get("/v1/artifacts", headers=auth).json()["artifacts"] == []


class TestCrossAgentHandoff:
    def test_a_to_b_by_artifact_then_c_by_head(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin

        # A: declared workspace, produces b.txt — never pushed to origin.
        agent_a = _create_with_workspace(client, auth, workspace_decl(repo, base))
        head_a = commit_in_agent(v1_env, agent_a["id"], "b.txt", "two\n")
        artifact = client.post(
            f"/v1/agents/{agent_a['id']}/artifacts", json={}, headers=auth
        ).json()["artifact"]

        # A's sandbox is gone — the durable artifact carries the work, and
        # A's slot frees for the next agent in the chain.
        assert client.delete(f"/v1/agents/{agent_a['id']}", headers=auth).status_code == 200

        # B: consumes A's work by artifact_id. B's prompt carries no source —
        # "two\n" appears nowhere in it; the artifact moves the bytes.
        prompt_b = "Continue the task; do not echo file contents."
        assert "two" not in prompt_b
        agent_b = _create_with_workspace(
            client,
            auth,
            workspace_decl(repo, base),
            handoff={"artifact_id": artifact["artifact_id"]},
            prompt={"text": prompt_b},
        )
        wd_b = workdir(v1_env, agent_b["id"])
        assert (wd_b / "b.txt").read_text() == "two\n"
        ws_b = client.get(f"/v1/agents/{agent_b['id']}/workspace", headers=auth).json()["workspace"]
        # The bundle pinned A's exact commit — B stands on it, not on base.
        assert ws_b["checkout_sha"] == ws_b["head_sha"] == head_a != base

        # Reviewer pins B's reviewed head to the exact handed-off commit.
        review = client.post(
            f"/v1/agents/{agent_b['id']}/workspace/review",
            json={"head_sha": head_a},
            headers=auth,
        )
        assert review.status_code == 200, review.text
        assert review.json()["workspace"]["reviewed_head_sha"] == head_a
        # A mismatched review pin is rejected explicitly.
        bad = client.post(
            f"/v1/agents/{agent_b['id']}/workspace/review",
            json={"head_sha": base},
            headers=auth,
        )
        assert bad.status_code == 409
        assert bad.json()["error"]["code"] == "head_sha_mismatch"

        # C: consumes by exact head_sha — publish A's commit to origin first
        # (head_sha handoff requires the commit reachable in the shared repo).
        host_git(wd_b, "push", "-q", str(repo), f"{head_a}:refs/heads/work-a")
        agent_c = _create_with_workspace(
            client,
            auth,
            workspace_decl(repo, base),
            handoff={"head_sha": head_a},
        )
        wd_c = workdir(v1_env, agent_c["id"])
        assert (wd_c / "b.txt").read_text() == "two\n"
        assert host_git(wd_c, "rev-parse", "HEAD") == head_a

    def test_head_sha_not_descending_from_base(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        host_git(repo, "checkout", "-q", "--orphan", "other")
        host_git(repo, "commit", "-qm", "D", "--allow-empty")
        other = host_git(repo, "rev-parse", "other")
        agent = create_agent(
            client,
            auth,
            workspace=workspace_decl(repo, base),
            handoff={"head_sha": other},
        )["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "ERROR"
        assert "base_sha_mismatch" in (run["error"] or {}).get("message", "")


class TestLiveHandoffRoute:
    def test_apply_artifact_onto_running_agent(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        agent_a = _create_with_workspace(client, auth, workspace_decl(repo, base))
        head_a = commit_in_agent(v1_env, agent_a["id"], "b.txt", "two\n")
        artifact = client.post(
            f"/v1/agents/{agent_a['id']}/artifacts", json={}, headers=auth
        ).json()["artifact"]

        agent_b = _create_with_workspace(client, auth, workspace_decl(repo, base))
        wait_idle(v1_env, agent_b["id"])
        resp = client.post(
            f"/v1/agents/{agent_b['id']}/handoff",
            json={"artifact_id": artifact["artifact_id"]},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["workspace"]["head_sha"] == head_a
        assert (workdir(v1_env, agent_b["id"]) / "b.txt").read_text() == "two\n"

    def test_handoff_requires_exactly_one_ref(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, origin: tuple[Path, str]
    ) -> None:
        repo, base = origin
        agent = _create_with_workspace(client, auth, workspace_decl(repo, base))
        wait_idle(v1_env, agent["id"])
        for body in ({}, {"artifact_id": "x", "head_sha": SHA_0}):
            resp = client.post(f"/v1/agents/{agent['id']}/handoff", json=body, headers=auth)
            assert resp.status_code == 400
            assert resp.json()["error"]["code"] == "workspace_invalid"
