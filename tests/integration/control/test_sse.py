"""SSE: keepalive, Last-Event-ID resume without drop/duplicate, kill tail on disconnect."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import uvicorn
from control.backend import LocalProcessBackend
from fastapi.testclient import TestClient
from tests.integration.control.conftest import AUTH
from tests.integration.control.test_api import wait_session


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_server(app) -> tuple[uvicorn.Server, threading.Thread, str]:
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
        raise AssertionError("control app did not start")
    return server, thread, base


def test_sse_keepalive_and_reconnect(control_env) -> None:
    app, _backend, _store = control_env
    server, thread, base = _start_server(app)
    try:
        with httpx.Client(base_url=base, timeout=10.0) as client:
            created = client.post("/api/sessions", json={"title": "sse"}, auth=AUTH)
            assert created.status_code == 201
            sid = created.json()["session_id"]
            wait_session(TestClient(app), sid, status="idle")
            posted = client.post(f"/api/sessions/{sid}/messages", json={"text": "hello"}, auth=AUTH)
            assert posted.status_code == 202
            wait_session(TestClient(app), sid, status="idle", min_turns=1)

            first_ids: list[int] = []
            first_keepalives = 0
            with client.stream("GET", f"/api/sessions/{sid}/events", auth=AUTH) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]
                deadline = time.monotonic() + 8
                for line in resp.iter_lines():
                    if line.startswith("id:"):
                        first_ids.append(int(line.split(":", 1)[1].strip()))
                    if line.startswith(": keepalive"):
                        first_keepalives += 1
                    if first_keepalives >= 1 and len(first_ids) >= 3:
                        break
                    if time.monotonic() > deadline:
                        break
            assert first_keepalives >= 1
            assert first_ids
            assert first_ids == list(range(first_ids[0], first_ids[-1] + 1))
            last = first_ids[-1]

            resume_ids: list[int] = []
            with client.stream(
                "GET",
                f"/api/sessions/{sid}/events",
                auth=AUTH,
                headers={"Last-Event-ID": str(last)},
            ) as resp:
                deadline = time.monotonic() + 8
                for line in resp.iter_lines():
                    if line.startswith("id:"):
                        resume_ids.append(int(line.split(":", 1)[1].strip()))
                    if resume_ids and resume_ids[-1] >= last + 2:
                        break
                    if len(resume_ids) >= 8:
                        break
                    if time.monotonic() > deadline:
                        break

            assert resume_ids, "resume should continue the stream"
            assert min(resume_ids) > last
            assert set(first_ids) & set(resume_ids) == set()
            combined = sorted(set(first_ids) | set(resume_ids))
            assert combined == list(range(combined[0], combined[-1] + 1))
            client.delete(f"/api/sessions/{sid}", auth=AUTH)
    finally:
        server.should_exit = True
        thread.join(timeout=2)


def test_sse_kills_tail_on_disconnect(control_env) -> None:
    app, backend, _store = control_env
    server, thread, base = _start_server(app)
    try:
        with httpx.Client(base_url=base, timeout=10.0) as client:
            sid = client.post("/api/sessions", json={"title": "tail"}, auth=AUTH).json()[
                "session_id"
            ]
            wait_session(TestClient(app), sid, status="idle")
            rec = app.state.plane.get(sid)
            assert rec is not None
            handle = rec.handle()
            assert handle is not None
            before = backend.poll(handle).active_processes
            with httpx.Client(base_url=base, timeout=10.0) as sse_client:
                with sse_client.stream("GET", f"/api/sessions/{sid}/events", auth=AUTH) as resp:
                    saw_ka = False
                    deadline = time.monotonic() + 5
                    for line in resp.iter_lines():
                        if line.startswith(": keepalive"):
                            saw_ka = True
                            break
                        if time.monotonic() > deadline:
                            break
                    assert saw_ka
                    deadline = time.monotonic() + 3
                    during = backend.poll(handle).active_processes
                    while during < before + 1 and time.monotonic() < deadline:
                        time.sleep(0.05)
                        during = backend.poll(handle).active_processes
                    assert during >= before + 1
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if backend.poll(handle).active_processes <= before:
                    break
                time.sleep(0.05)
            assert backend.poll(handle).active_processes <= before
            client.delete(f"/api/sessions/{sid}", auth=AUTH)
    finally:
        server.should_exit = True
        thread.join(timeout=2)


def test_sse_periodic_keepalive(control_env) -> None:
    app, _backend, _store = control_env
    server, thread, base = _start_server(app)
    try:
        with httpx.Client(base_url=base, timeout=10.0) as client:
            sid = client.post("/api/sessions", json={"title": "ka"}, auth=AUTH).json()["session_id"]
            wait_session(TestClient(app), sid, status="idle")
            keepalives = 0
            started = time.monotonic()
            with client.stream("GET", f"/api/sessions/{sid}/events", auth=AUTH) as resp:
                for line in resp.iter_lines():
                    if line.startswith(": keepalive"):
                        keepalives += 1
                    if keepalives >= 2:
                        break
                    if time.monotonic() - started > 5:
                        break
            assert keepalives >= 2
            assert time.monotonic() - started < 5
            client.delete(f"/api/sessions/{sid}", auth=AUTH)
    finally:
        server.should_exit = True
        thread.join(timeout=2)


def test_backend_type_is_local(control_env) -> None:
    _app, backend, _store = control_env
    assert isinstance(backend, LocalProcessBackend)
