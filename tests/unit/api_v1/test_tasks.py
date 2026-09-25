"""SOR-222/223: public Task API — preflight, create, list, get.

The repo probe uses a real ``file://`` repo (offline ``git ls-remote`` —
the same code path non-github remotes take) so resolution is exercised
end to end without network access.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.unit.api_v1.conftest import V1Env, seed_account, wait_run

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git binary unavailable")


@pytest.fixture
def git_repo(tmp_path: Path) -> tuple[str, str]:
    """A real local repo → ``(file:// url, HEAD sha)``."""
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
    """The seeded codex account needs auth material to pass the auth check."""
    v1_env.registry.put_credential_blob(
        "acct-codex-1",
        {"provider": "codex", "files": {".codex/auth.json": "{}"}},
    )
    return v1_env


def _task_body(repo_url: str | None = None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"prompt": {"text": "Create hello.txt."}}
    if repo_url is not None:
        body["source"] = {"repo": repo_url}
    body.update(extra)
    return body


# ---------------------------------------------------------------------------
# preflight — advisory resolution, always 200 on a well-formed declaration
# ---------------------------------------------------------------------------


@needs_git
def test_preflight_resolves_source_and_execution(
    client: TestClient, auth: dict[str, str], credentialed: V1Env, git_repo
) -> None:
    url, sha = git_repo
    resp = client.post("/v1/tasks/preflight", json=_task_body(url), headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    resolved = body["resolved"]
    assert resolved["source"]["base_ref"] == "main"
    assert resolved["source"]["base_sha"] == sha
    assert resolved["source"]["kind"] == "local"
    exe = resolved["execution"]
    assert exe["provider"] == "codex"
    assert exe["account_id"] == "acct-codex-1"
    assert exe["model"] == "gpt-5.6-luna"
    # Resolved evidence: requested vs resolved per field.
    evidence = exe["evidence"]
    assert evidence["provider"]["requested"] == "auto"
    assert evidence["account_id"]["source"] == "lru"
    assert evidence["model"]["resolved"] == "gpt-5.6-luna"
    # Every check is a verdict, never a reservation.
    assert all(c["status"] in ("pass", "warn") for c in body["checks"])
    check_names = {c["name"] for c in body["checks"]}
    assert {"source.repo", "source.ref", "source.sha", "execution.account"} <= check_names


@needs_git
def test_preflight_reports_failures_as_200_ok_false(
    client: TestClient, auth: dict[str, str], v1_env: V1Env, git_repo
) -> None:
    url, _ = git_repo
    # acct-codex-1 has no auth material (fixture not applied) → ineligible.
    resp = client.post("/v1/tasks/preflight", json=_task_body(url), headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "provider_exhausted"
    candidates = body["resolved"]["execution"]["candidates"]
    assert candidates[0]["account_id"] == "acct-codex-1"
    assert "no_credential" in candidates[0]["reasons"]
    # No agent was provisioned by the advisory call.
    assert client.get("/v1/agents", headers=auth).json()["agents"] == []


@needs_git
def test_preflight_unreachable_repo(
    client: TestClient, auth: dict[str, str], v1_env: V1Env, tmp_path: Path
) -> None:
    resp = client.post(
        "/v1/tasks/preflight",
        json=_task_body(f"file://{tmp_path / 'missing-repo'}"),
        headers=auth,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "repo_unavailable"
    assert any(c["status"] == "fail" for c in body["checks"])


@needs_git
def test_preflight_github_permission_preflight(
    client: TestClient, auth: dict[str, str], credentialed: V1Env
) -> None:
    """A github.com source runs the authorization/permission checks."""

    class DeniedResolver:
        def default_branch(self, repo):
            return "main"

        def resolve_ref(self, repo, ref):
            return "a" * 40

        def access(self, repo):
            return {"read": "no", "push": "unknown", "source": "github_api"}

    credentialed.app.state.repo_resolver = DeniedResolver()
    resp = client.post(
        "/v1/tasks/preflight",
        json=_task_body("https://github.com/private/repo"),
        headers=auth,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "repo_unavailable"
    assert body["resolved"] is None
    assert any(c["name"] == "github.read" and c["status"] == "fail" for c in body["checks"])


def test_preflight_validation_errors(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Canonical-but-disabled provider → invalid_provider refusal (200 advisory).
    monkeypatch.setenv("SBX_PROVIDERS", "codex")
    resp = client.post(
        "/v1/tasks/preflight",
        json=_task_body(execution={"provider": "grok"}),
        headers=auth,
    )
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == "invalid_provider"
    # Malformed body → the V1Route-shaped 400 (SOR-226: generic malformed
    # requests are invalid_request; invalid_provider is provider-field only).
    resp = client.post("/v1/tasks/preflight", json={"prompt": {}}, headers=auth)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# create — authoritative reserve + requested-vs-resolved persistence
# ---------------------------------------------------------------------------


@needs_git
def test_create_task_end_to_end(
    client: TestClient, auth: dict[str, str], credentialed: V1Env, git_repo
) -> None:
    url, sha = git_repo
    resp = client.post("/v1/tasks", json=_task_body(url), headers=auth)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    task, agent, run = body["task"], body["agent"], body["run"]
    assert task["id"].startswith("task_")
    assert task["status"] == "queued"
    assert task["agent_id"] == agent["id"]
    assert task["run_id"] == run["id"]
    # Persisted request is verbatim — no base_sha/git booleans were sent.
    assert task["request"]["prompt"]["text"] == "Create hello.txt."
    assert "base_sha" not in task["request"]["source"]
    # Resolved: exact sha + canonical ref + the picked account/model.
    assert task["resolved"]["source"]["base_sha"] == sha
    assert task["resolved"]["source"]["base_ref"] == "main"
    assert task["resolved"]["execution"]["account_id"] == "acct-codex-1"
    assert task["resolved"]["execution"]["model"] == "gpt-5.6-luna"
    # The created agent runs the exact /v1 machinery.
    finished = wait_run(client, auth, agent["id"], run["id"])
    assert finished["status"] == "FINISHED"
    # Status derives live from the run.
    detail = client.get(f"/v1/tasks/{task['id']}", headers=auth)
    assert detail.status_code == 200
    assert detail.json()["task"]["status"] == "finished"
    assert detail.json()["agent"]["id"] == agent["id"]
    assert detail.json()["run"]["status"] == "FINISHED"
    listing = client.get("/v1/tasks", headers=auth).json()
    assert [t["id"] for t in listing["tasks"]] == [task["id"]]


@needs_git
def test_create_task_without_source(
    client: TestClient, auth: dict[str, str], credentialed: V1Env
) -> None:
    """A prompt-only task resolves execution and runs with no workspace."""
    resp = client.post("/v1/tasks", json=_task_body(), headers=auth)
    assert resp.status_code == 201, resp.text
    task = resp.json()["task"]
    assert task["resolved"]["source"] is None
    assert task["resolved"]["execution"]["account_id"] == "acct-codex-1"


@needs_git
def test_create_task_idempotent_replay(
    client: TestClient, auth: dict[str, str], credentialed: V1Env, git_repo
) -> None:
    url, _ = git_repo
    headers = {**auth, "Idempotency-Key": "create-1"}
    first = client.post("/v1/tasks", json=_task_body(url), headers=headers)
    assert first.status_code == 201, first.text
    replay = client.post("/v1/tasks", json=_task_body(url), headers=headers)
    assert replay.status_code in (200, 201)
    assert replay.json()["task"]["id"] == first.json()["task"]["id"]
    assert replay.json()["agent"]["id"] == first.json()["agent"]["id"]
    agents = client.get("/v1/agents", headers=auth).json()["agents"]
    assert len(agents) == 1
    # Different body, same key → conflict.
    conflict = client.post("/v1/tasks", json=_task_body(url, name="different"), headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


@needs_git
def test_create_task_delivery_pull_request_resolves_git(
    client: TestClient, auth: dict[str, str], credentialed: V1Env, git_repo
) -> None:
    url, _ = git_repo
    resp = client.post(
        "/v1/tasks",
        json=_task_body(
            url,
            delivery={
                "branch": "feat/x",
                "pull_request": {"title": "Add hello", "draft": True},
            },
        ),
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    git = resp.json()["task"]["resolved"]["git"]
    assert git["auto_create_pr"] is True
    assert git["push"] is True
    assert git["draft"] is True
    assert git["branch"] == "feat/x"


@needs_git
def test_create_task_blocked_accounts_fail_authoritatively(
    client: TestClient, auth: dict[str, str], v1_env: V1Env, git_repo
) -> None:
    url, _ = git_repo
    resp = client.post("/v1/tasks", json=_task_body(url), headers=auth)
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "provider_exhausted"
    # The task is not persisted on refusal.
    assert client.get("/v1/tasks", headers=auth).json()["tasks"] == []


@needs_git
def test_create_task_repo_unavailable(
    client: TestClient, auth: dict[str, str], credentialed: V1Env, tmp_path: Path
) -> None:
    resp = client.post(
        "/v1/tasks",
        json=_task_body(f"file://{tmp_path / 'missing-repo'}"),
        headers=auth,
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "repo_unavailable"


def test_task_requires_agents_scope_and_auth(
    client: TestClient, auth: dict[str, str], v1_env: V1Env
) -> None:
    assert client.post("/v1/tasks", json=_task_body()).status_code == 401
    assert client.get("/v1/tasks").status_code == 401
    assert client.get("/v1/tasks/task_nope", headers=auth).status_code == 404


# ---------------------------------------------------------------------------
# auto failover + named account pinning through the API
# ---------------------------------------------------------------------------


@needs_git
def test_task_auto_account_skips_ineligible(
    client: TestClient, auth: dict[str, str], credentialed: V1Env, git_repo
) -> None:
    """A cooling/unusable sibling never blocks an eligible account's pick."""
    url, _ = git_repo
    seed_account(
        credentialed, "acct-codex-busy", provider="codex", models=("gpt-9",), secret_name="s"
    )
    credentialed.registry.set_running("acct-codex-busy", 1)
    resp = client.post("/v1/tasks", json=_task_body(url), headers=auth)
    assert resp.status_code == 201, resp.text
    exe = resp.json()["task"]["resolved"]["execution"]
    assert exe["account_id"] == "acct-codex-1"
    reasons = {c["account_id"]: c["reasons"] for c in exe["candidates"]}
    assert "at_capacity" in reasons["acct-codex-busy"]


@needs_git
def test_task_auto_applies_lru_pick_not_listing_order(
    client: TestClient, auth: dict[str, str], credentialed: V1Env, git_repo
) -> None:
    """The recorded LRU pick is the account that actually runs.

    Listing order (sorted account id) and LRU order disagree here:
    ``acct-codex-1`` sorts first but was used most recently, so ``auto``
    must create on the never-used account — and the durable ``resolved``
    must agree with the agent that was created.
    """
    url, _ = git_repo
    seed_account(
        credentialed,
        "acct-codex-2",
        provider="codex",
        models=("gpt-5.6-luna",),
        secret_name="s",
    )
    credentialed.registry.touch("acct-codex-1", datetime.now(UTC).isoformat())
    resp = client.post("/v1/tasks", json=_task_body(url), headers=auth)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    exe = body["task"]["resolved"]["execution"]
    assert exe["account_id"] == "acct-codex-2"
    assert exe["evidence"]["account_id"]["source"] == "lru"
    assert body["agent"]["account_id"] == "acct-codex-2"


@needs_git
def test_task_named_account_pinned(
    client: TestClient, auth: dict[str, str], credentialed: V1Env, git_repo
) -> None:
    url, sha = git_repo
    resp = client.post(
        "/v1/tasks",
        json=_task_body(url, execution={"account_id": "acct-codex-1"}),
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    exe = resp.json()["task"]["resolved"]["execution"]
    assert exe["account_id"] == "acct-codex-1"
    assert exe["evidence"]["account_id"]["source"] == "requested"


@needs_git
def test_task_exact_sha_source(
    client: TestClient, auth: dict[str, str], credentialed: V1Env, git_repo
) -> None:
    url, sha = git_repo
    resp = client.post("/v1/tasks", json=_task_body(url).copy() | {}, headers=auth)
    body = _task_body(url)
    body["source"]["ref"] = sha
    resp = client.post("/v1/tasks", json=body, headers=auth)
    assert resp.status_code == 201, resp.text
    assert resp.json()["task"]["resolved"]["source"]["base_sha"] == sha
