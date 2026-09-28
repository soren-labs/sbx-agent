"""``/v2`` API tests reuse the ``/v1`` fixture stack (same app, same fakes).

Sessions are created through the task engine, so the seeded codex account
needs auth material — ``client`` therefore pulls ``credentialed`` so every
test that makes calls is covered.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# Re-export the v1 fixtures: v1_env / auth / admin_auth / live_base are
# imported names — pytest registers them for this package.
from tests.unit.api_v1.conftest import (  # noqa: F401
    V1Env,
    admin_auth,
    auth,
    live_base,
    v1_env,
)

GIT = shutil.which("git")


@pytest.fixture
def credentialed(v1_env: V1Env) -> V1Env:  # noqa: F811
    """The seeded codex account needs auth material for the task filter.

    Also raises the live-agent cap: v2 list/pagination tests hold several
    concurrent sessions where the v1 default assumes one-at-a-time.
    """
    v1_env.registry.put_credential_blob(
        "acct-codex-1",
        {"provider": "codex", "files": {".codex/auth.json": "{}"}},
    )
    v1_env.app.state.plane.max_concurrent = 64
    return v1_env


@pytest.fixture
def client(credentialed: V1Env) -> Iterator[TestClient]:
    with TestClient(credentialed.app) as test_client:
        yield test_client


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


def create_session(client: TestClient, headers: dict[str, str], **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "prompt": "Create hello.txt in the workspace.",
        "execution": {"provider": "codex"},
    }
    body.update(overrides)
    resp = client.post("/v2/sessions", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def wait_session(
    client: TestClient,
    headers: dict[str, str],
    session_id: str,
    *statuses: str,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Block until the session's public status settles into ``statuses``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/v2/sessions/{session_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["session"]["status"] in statuses:
            return body
        time.sleep(0.1)
    raise AssertionError(f"session {session_id} never reached {statuses}")
