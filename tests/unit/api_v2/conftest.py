"""Fixtures for ``/v2`` API tests — the same seams as ``/v1``.

The V1 env (real ControlPlane + LocalProcessBackend + stub_runner + WP0
fakes) is imported wholesale; the V2 surface is a facade over the same
stores, so a session created through either version is visible on both.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.unit.api_v1.conftest import (  # noqa: F401 — re-exported fixtures
    V1Env,
    admin_auth,
    auth,
    client,
    live_base,
    seed_account,
    v1_env,
    wait_run,
    wait_sandbox,
    wait_status,
)


@pytest.fixture
def credentialed(v1_env: V1Env) -> V1Env:
    """The seeded codex account needs auth material to pass the auth check."""
    v1_env.registry.put_credential_blob(
        "acct-codex-1",
        {"provider": "codex", "files": {".codex/auth.json": "{}"}},
    )
    return v1_env


@pytest.fixture
def fast_events(v1_env: V1Env) -> V1Env:
    """Tighten the V2 feed poller so mid-run transitions are observable."""
    v1_env.app.state.v2_events_poll_s = 0.05
    return v1_env


def session_body(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"prompt": "Create hello.txt."}
    body.update(extra)
    return body


def create_session(client: TestClient, auth: dict[str, str], **extra: Any) -> dict[str, Any]:
    resp = client.post("/v2/sessions", json=session_body(**extra), headers=auth)
    assert resp.status_code == 201, resp.text
    return resp.json()["session"]


def wait_session(
    client: TestClient,
    auth: dict[str, str],
    session_id: str,
    *statuses: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Block until the session's public status reaches one of ``statuses``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/v2/sessions/{session_id}", headers=auth)
        assert resp.status_code == 200, resp.text
        session = resp.json()["session"]
        if session["status"] in statuses:
            return session
        time.sleep(0.1)
    raise AssertionError(f"session {session_id} never reached {statuses}")


def iter_sse(response: Any) -> Iterator[dict[str, Any]]:
    """Parse an httpx SSE body into ``{id,event,data}`` frames."""
    frame: dict[str, Any] = {}
    for line in response.iter_lines():
        if not line:
            if frame:
                yield frame
                frame = {}
            continue
        if line.startswith(": "):
            continue
        field, _, value = line.partition(": ")
        if field == "data":
            frame["data"] = json.loads(value)
        else:
            frame[field] = value
    if frame:
        yield frame


__all__ = [
    "V1Env",
    "auth",
    "admin_auth",
    "client",
    "live_base",
    "seed_account",
    "v1_env",
    "wait_run",
    "wait_sandbox",
    "wait_status",
    "credentialed",
    "fast_events",
    "session_body",
    "create_session",
    "wait_session",
    "iter_sse",
]
