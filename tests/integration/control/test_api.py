"""httpx/TestClient against the real control plane (LocalProcessBackend + stub_runner)."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient
from tests.integration.control.conftest import AUTH


def wait_session(
    client: TestClient,
    sid: str,
    *,
    status: str | None = None,
    min_turns: int | None = None,
    timeout: float = 15.0,
) -> dict:
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        resp = client.get(f"/api/sessions/{sid}", auth=AUTH)
        assert resp.status_code == 200, resp.text
        last = resp.json()
        ok = True
        if status is not None:
            ok = ok and last["status"] == status
        if min_turns is not None:
            ok = ok and last["turns"] >= min_turns
        if ok:
            return last
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for session {sid}: {last}")


def test_unauthorized(client: TestClient) -> None:
    missing = client.get("/api/sessions")
    assert missing.status_code == 401
    assert missing.json()["code"] == 401
    assert missing.headers.get("www-authenticate", "").lower().startswith("basic")
    wrong = client.get("/api/sessions", auth=("sbx", "nope"))
    assert wrong.status_code == 401
    assert wrong.json() == {"error": "unauthorized", "code": 401}


def test_create_get_list_delete(client: TestClient) -> None:
    created = client.post(
        "/api/sessions", json={"title": "one", "model": "gpt-5.6-luna"}, auth=AUTH
    )
    assert created.status_code == 201
    sid = created.json()["session_id"]
    got = wait_session(client, sid, status="idle")
    assert got["id"] == sid
    assert got["title"] == "one"
    assert got["model"] == "gpt-5.6-luna"
    assert got["turns"] == 0
    assert set(got["usage"]) >= {"input_tokens", "cached_input_tokens", "output_tokens"}
    assert got["cost_estimate_usd"] >= 0
    assert got["sandbox_seconds"] >= 0
    listed = client.get("/api/sessions", auth=AUTH)
    assert listed.status_code == 200
    assert any(item["id"] == sid for item in listed.json())
    deleted = client.delete(f"/api/sessions/{sid}", auth=AUTH)
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "closed"
    still = client.get(f"/api/sessions/{sid}", auth=AUTH)
    assert still.status_code == 200
    assert still.json()["status"] == "closed"
    assert still.json()["messages"] == []


def test_create_without_body_defaults(client: TestClient) -> None:
    created = client.post("/api/sessions", auth=AUTH)
    assert created.status_code == 201
    sid = created.json()["session_id"]
    got = wait_session(client, sid, status="idle")
    assert got["title"] == "untitled"
    assert got["model"] == "gpt-5.6-luna"
    client.delete(f"/api/sessions/{sid}", auth=AUTH)


def test_404(client: TestClient) -> None:
    r = client.get("/api/sessions/does-not-exist", auth=AUTH)
    assert r.status_code == 404
    assert r.json()["code"] == 404
    r = client.post("/api/sessions/does-not-exist/messages", json={"text": "hi"}, auth=AUTH)
    assert r.status_code == 404
    r = client.post("/api/sessions/does-not-exist/stop", auth=AUTH)
    assert r.status_code == 404
    r = client.delete("/api/sessions/does-not-exist", auth=AUTH)
    assert r.status_code == 404
    r = client.get("/api/sessions/does-not-exist/events", auth=AUTH)
    assert r.status_code == 404


def test_concurrency_limit_429(client: TestClient, control_env) -> None:
    # Pin the plane cap — the default now resolves SBX_MAX_CONCURRENT → 8
    # (SOR-271 round-4); this spec exercises cap enforcement itself.
    control_env[0].state.plane.max_concurrent = 2
    a = client.post("/api/sessions", json={"title": "a"}, auth=AUTH)
    b = client.post("/api/sessions", json={"title": "b"}, auth=AUTH)
    assert a.status_code == 201
    assert b.status_code == 201
    c = client.post("/api/sessions", json={"title": "c"}, auth=AUTH)
    assert c.status_code == 429
    assert c.json()["code"] == 429
    assert c.json()["error"] == "concurrency_limit"
    client.delete(f"/api/sessions/{a.json()['session_id']}", auth=AUTH)
    d = client.post("/api/sessions", json={"title": "d"}, auth=AUTH)
    assert d.status_code == 201
    client.delete(f"/api/sessions/{b.json()['session_id']}", auth=AUTH)
    client.delete(f"/api/sessions/{d.json()['session_id']}", auth=AUTH)


def test_message_409_when_turn_running(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "hang")
    sid = client.post("/api/sessions", json={"title": "m"}, auth=AUTH).json()["session_id"]
    wait_session(client, sid, status="idle")
    first = client.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, auth=AUTH)
    assert first.status_code == 202
    assert "turn_id" in first.json()
    second = client.post(f"/api/sessions/{sid}/messages", json={"text": "again"}, auth=AUTH)
    assert second.status_code == 409
    assert second.json()["code"] == 409
    assert second.json()["error"] == "turn_in_progress"
    stop = client.post(f"/api/sessions/{sid}/stop", auth=AUTH)
    assert stop.status_code == 202
    wait_session(client, sid, status="idle")
    closed = client.delete(f"/api/sessions/{sid}", auth=AUTH)
    assert closed.json()["status"] == "closed"
    again = client.post(f"/api/sessions/{sid}/messages", json={"text": "nope"}, auth=AUTH)
    assert again.status_code == 409
