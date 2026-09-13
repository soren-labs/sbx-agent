"""httpx against mock_api: REST errors, SSE frames, keepalive comments."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from tests.fakes import mock_api
from tests.fakes.mock_api import app, reset_state

AUTH = ("sbx", "sbx")


@pytest.fixture(autouse=True)
def _clean_sessions() -> Iterator[None]:
    reset_state()
    yield
    reset_state()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_unauthorized(client: TestClient) -> None:
    r = client.get("/api/sessions")
    assert r.status_code == 401
    body = r.json()
    assert body["code"] == 401


def test_create_get_list_delete(client: TestClient) -> None:
    created = client.post("/api/sessions", json={"title": "one", "model": "gpt-5"}, auth=AUTH)
    assert created.status_code == 201
    sid = created.json()["session_id"]
    got = client.get(f"/api/sessions/{sid}", auth=AUTH)
    assert got.status_code == 200
    session = got.json()
    assert session["id"] == sid
    assert session["status"] in {"creating", "idle"}
    assert session["title"] == "one"
    assert set(session["usage"]) == {"input_tokens", "cached_input_tokens", "output_tokens"}
    listed = client.get("/api/sessions", auth=AUTH)
    assert any(item["id"] == sid for item in listed.json())
    deleted = client.delete(f"/api/sessions/{sid}", auth=AUTH)
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "closed"


def test_concurrency_limit_429(client: TestClient) -> None:
    a = client.post("/api/sessions", json={"title": "a"}, auth=AUTH)
    b = client.post("/api/sessions", json={"title": "b"}, auth=AUTH)
    assert a.status_code == 201
    assert b.status_code == 201
    c = client.post("/api/sessions", json={"title": "c"}, auth=AUTH)
    assert c.status_code == 429
    assert c.json()["code"] == 429
    client.delete(f"/api/sessions/{a.json()['session_id']}", auth=AUTH)
    d = client.post("/api/sessions", json={"title": "d"}, auth=AUTH)
    assert d.status_code == 201


def test_message_409_when_turn_running(client: TestClient) -> None:
    sid = client.post("/api/sessions", json={"title": "m"}, auth=AUTH).json()["session_id"]
    first = client.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, auth=AUTH)
    assert first.status_code == 202
    assert "turn_id" in first.json()
    second = client.post(f"/api/sessions/{sid}/messages", json={"text": "again"}, auth=AUTH)
    assert second.status_code == 409
    assert second.json()["code"] == 409
    stop = client.post(f"/api/sessions/{sid}/stop", auth=AUTH)
    assert stop.status_code == 202


def test_message_409_when_session_closed(client: TestClient) -> None:
    sid = client.post("/api/sessions", json={"title": "gone"}, auth=AUTH).json()["session_id"]
    client.delete(f"/api/sessions/{sid}", auth=AUTH)
    again = client.post(f"/api/sessions/{sid}/messages", json={"text": "nope"}, auth=AUTH)
    assert again.status_code == 409
    body = again.json()
    assert body["code"] == 409
    assert body["error"] == "session_not_runnable"


def test_404(client: TestClient) -> None:
    r = client.get("/api/sessions/does-not-exist", auth=AUTH)
    assert r.status_code == 404
    assert r.json()["code"] == 404


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_sse_frames_and_keepalive_via_httpx() -> None:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base}/api/sessions", auth=AUTH, timeout=0.2)
            break
        except httpx.TransportError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        raise AssertionError("mock_api did not start")

    try:
        with httpx.Client(base_url=base, timeout=10.0) as client:
            created = client.post("/api/sessions", json={"title": "sse"}, auth=AUTH)
            assert created.status_code == 201
            sid = created.json()["session_id"]
            data_frames = 0
            keepalive = 0
            event_names: list[str] = []
            with client.stream("GET", f"/api/sessions/{sid}/events", auth=AUTH) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]
                for line in resp.iter_lines():
                    if line.startswith(": keepalive"):
                        keepalive += 1
                    elif line.startswith("event:"):
                        event_names.append(line.split(":", 1)[1].strip())
                    elif line.startswith("data:"):
                        payload = json.loads(line.split(":", 1)[1].strip())
                        assert "type" in payload
                        data_frames += 1
                    if data_frames >= 3 and keepalive >= 1:
                        break
            assert data_frames >= 3
            assert keepalive >= 1
            assert "thread.started" in event_names

            with client.stream(
                "GET",
                f"/api/sessions/{sid}/events",
                auth=AUTH,
                headers={"Last-Event-ID": "1"},
            ) as resp:
                saw_id: int | None = None
                for line in resp.iter_lines():
                    if line.startswith("id:"):
                        saw_id = int(line.split(":", 1)[1].strip())
                        if saw_id > 1:
                            break
                assert saw_id is not None and saw_id > 1
    finally:
        server.should_exit = True
        thread.join(timeout=2)


def test_module_entrypoint_has_main() -> None:
    assert callable(mock_api.main)
