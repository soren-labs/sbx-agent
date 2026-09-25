"""SOR-211 integration: the control plane serves the Console same-origin
with /v1, and the one-time grant handoff mints a working admin key."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from control.app import create_app
from control.backend import LocalProcessBackend
from control.store import InMemoryStore
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB_DIR = REPO_ROOT / "web"


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SBX_BACKEND", "local")
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", "sbx_integration_bootstrap")
    app = create_app(
        backend=LocalProcessBackend(),
        store=InMemoryStore(),
        runner_cmd=[sys.executable, "-m", "runtime.runner"],
        basic_user="sbx",
        basic_password="sbx",
        keepalive_s=0.4,
    )
    with TestClient(app) as c:
        yield c


def test_console_served_same_origin(app_client: TestClient) -> None:
    resp = app_client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # the Console's app shell
    assert "sbx" in resp.text.lower()


def test_console_serving_skipped_without_web_dir(app_client: TestClient, monkeypatch) -> None:
    # Library-only installs (no web/) must still serve /v1.
    monkeypatch.setenv("SBX_WEB_DIR", "/nonexistent-web")
    app = create_app(
        backend=LocalProcessBackend(),
        store=InMemoryStore(),
        keepalive_s=0.4,
    )
    c = TestClient(app)
    assert c.get("/").status_code == 404
    assert c.get("/v1/me").status_code == 401  # v1 alive, console absent


def test_grant_handoff_end_to_end(app_client: TestClient) -> None:
    auth = {"Authorization": "Bearer sbx_integration_bootstrap"}
    mint = app_client.post("/v1/console/grant", headers=auth)
    assert mint.status_code == 201
    grant = mint.json()["grant"]

    # Exchange (unauthenticated) yields a working admin key.
    ex = app_client.post("/v1/console/exchange", json={"grant": grant})
    assert ex.status_code == 201
    key = ex.json()["key"]
    assert app_client.get("/v1/me", headers={"Authorization": f"Bearer {key}"}).status_code == 200

    # Single use: replay is rejected.
    again = app_client.post("/v1/console/exchange", json={"grant": grant})
    assert again.status_code == 401
    assert again.json()["error"]["code"] == "grant_invalid"
