"""Helpers for WP2-F cloud-free integration tests."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from control.store import InMemoryStore
from fastapi.testclient import TestClient
from tests.integration.cloud_free.conftest import AUTH

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "events"


def wait_session(
    client: TestClient | httpx.Client,
    sid: str,
    *,
    status: str | None = None,
    min_turns: int | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
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


def sandbox_root(store: InMemoryStore, sid: str) -> Path:
    rec = store.get(sid)
    assert rec is not None
    assert rec.sandbox_root is not None
    return Path(rec.sandbox_root)


def parse_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def fixture_usage(scenario: str) -> dict[str, int]:
    for raw in (FIXTURE_DIR / f"{scenario}.jsonl").read_text(encoding="utf-8").splitlines():
        obj = json.loads(raw)
        if obj.get("type") == "turn.completed" and isinstance(obj.get("usage"), dict):
            return {key: int(value) for key, value in obj["usage"].items()}
    raise AssertionError(f"no turn.completed usage in {scenario}.jsonl")


def load_turn(root: Path, n: int) -> dict[str, Any]:
    return json.loads((root / "turns" / f"{n}.json").read_text(encoding="utf-8"))


def cleanup_sessions(client: TestClient | httpx.Client, *sids: str) -> None:
    for sid in sids:
        try:
            client.post(f"/api/sessions/{sid}/stop", auth=AUTH)
        except Exception:
            pass
        try:
            client.delete(f"/api/sessions/{sid}", auth=AUTH)
        except Exception:
            pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_server(app: object) -> tuple[uvicorn.Server, threading.Thread, str]:
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True, name="sbx-wp2f-uvicorn")
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


def stop_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=2)
