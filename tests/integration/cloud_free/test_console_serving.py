"""SOR-211 + SOR-266 integration: the control plane serves the V2 React
Console (``console/dist``) same-origin with /v1, the legacy ``web/`` UI is
never the default product UI at ``/``, and the one-time grant handoff
mints a working admin key."""

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
CONSOLE_DIST = REPO_ROOT / "console" / "dist"


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SBX_BACKEND", "local")
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", "sbx_integration_bootstrap")
    # SOR-266: when a real console/dist exists in the checkout it is served
    # at "/"; otherwise stand in a minimal fixture so the console lane is
    # exercised deterministically regardless of build state.
    monkeypatch.delenv("SBX_WEB_DIR", raising=False)
    if not CONSOLE_DIST.is_dir():
        monkeypatch.setenv("SBX_CONSOLE_DIR", str(_write_console_fixture(tmp_path)))
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


def _write_console_fixture(tmp_path: Path) -> Path:
    dist = tmp_path / "console-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<html><head><meta name="sbx-build-sha" content="abc123">'
        '</head><body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    (dist / "assets" / "index-abc123.js").write_text("/* fixture bundle */", encoding="utf-8")
    return dist


def test_console_served_same_origin(app_client: TestClient) -> None:
    resp = app_client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # the Console's app shell
    assert "sbx" in resp.text.lower()


def test_console_spa_fallback_and_api_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """React Router deep links serve index.html; API prefixes never do."""
    monkeypatch.setenv("SBX_BACKEND", "local")
    monkeypatch.setenv("SBX_CONSOLE_DIR", str(_write_console_fixture(tmp_path)))
    monkeypatch.delenv("SBX_WEB_DIR", raising=False)
    app = create_app(backend=LocalProcessBackend(), store=InMemoryStore(), keepalive_s=0.4)
    c = TestClient(app)

    root = c.get("/")
    assert root.status_code == 200 and 'id="root"' in root.text

    for path in ("/sessions", "/sessions/sess_123", "/integrations", "/settings"):
        resp = c.get(path)
        assert resp.status_code == 200, path
        assert 'id="root"' in resp.text, path
        assert "text/html" in resp.headers["content-type"]

    # Real asset serves with immutable cache; a missing asset stays 404.
    asset = c.get("/assets/index-abc123.js")
    assert asset.status_code == 200
    assert "immutable" in asset.headers.get("cache-control", "")
    assert c.get("/assets/missing.js").status_code == 404

    # API paths never fall back to the SPA shell.
    assert "text/html" not in c.get("/v2/does-not-exist").headers.get("content-type", "")
    assert c.get("/v2/does-not-exist").status_code in (401, 404)


def test_console_dir_explicit_missing_fails_loudly(monkeypatch, tmp_path: Path) -> None:
    """A deploy must never silently fall back to web/ at root (SOR-266)."""
    monkeypatch.setenv("SBX_BACKEND", "local")
    monkeypatch.setenv("SBX_CONSOLE_DIR", str(tmp_path / "missing-dist"))
    with pytest.raises(RuntimeError, match="SBX_CONSOLE_DIR"):
        create_app(backend=LocalProcessBackend(), store=InMemoryStore(), keepalive_s=0.4)


def test_legacy_web_only_at_marked_path(monkeypatch, tmp_path: Path) -> None:
    """Without a console build, web/ stays reachable only under /legacy —
    never the default product UI at root (SOR-266)."""
    if (REPO_ROOT / "console" / "dist").is_dir():
        pytest.skip("checkout has a built console/dist — root is the V2 console")
    monkeypatch.setenv("SBX_BACKEND", "local")
    monkeypatch.delenv("SBX_CONSOLE_DIR", raising=False)
    monkeypatch.delenv("SBX_WEB_DIR", raising=False)
    app = create_app(backend=LocalProcessBackend(), store=InMemoryStore(), keepalive_s=0.4)
    c = TestClient(app)
    assert c.get("/").status_code == 404
    legacy = c.get("/legacy/")
    assert legacy.status_code == 200
    assert "text/html" in legacy.headers["content-type"]


def test_explicit_web_dir_is_legacy_root_lane(monkeypatch, tmp_path: Path) -> None:
    """SBX_WEB_DIR (explicit) restores the legacy UI at root for e2e/dev
    when no console build is present."""
    if (REPO_ROOT / "console" / "dist").is_dir():
        pytest.skip("checkout has a built console/dist — root is the V2 console")
    monkeypatch.setenv("SBX_BACKEND", "local")
    monkeypatch.delenv("SBX_CONSOLE_DIR", raising=False)
    monkeypatch.setenv("SBX_WEB_DIR", str(WEB_DIR))
    app = create_app(backend=LocalProcessBackend(), store=InMemoryStore(), keepalive_s=0.4)
    c = TestClient(app)
    assert c.get("/").status_code == 200
    assert c.get("/legacy/").status_code == 200


def test_console_serving_skipped_without_web_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Library-only installs (no console/, no web/) must still serve /v1.
    if (REPO_ROOT / "console" / "dist").is_dir():
        pytest.skip("checkout has a built console/dist — root is the V2 console")
    monkeypatch.setenv("SBX_BACKEND", "local")
    monkeypatch.delenv("SBX_CONSOLE_DIR", raising=False)
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
