"""Helpers for WP2-H real Modal e2e. Never print secret values."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = Path(__file__).resolve().parent / "artifacts"
DEFAULT_CONTROL_URL = "https://sorenlab2026--sbx-control-fastapi-app.modal.run"
CREATE_TIMEOUT_S = 90.0
TURN_TIMEOUT_S = 420.0
POLL_S = 1.0

JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
SK_RE = re.compile(r"sk-[A-Za-z0-9]{10,}")

TURN1_PROMPT = """Work only in /work.

Create exactly these two files and do not fix the bug in this turn.

File adder.py:
def add(a, b):
    # INTENTIONAL BUG (leave for the next turn): subtraction instead of addition.
    return a - b

File test_adder.py:
import unittest
from adder import add

class TestAdd(unittest.TestCase):
    def test_add_positive(self):
        self.assertEqual(add(2, 3), 5)

    def test_add_zero(self):
        self.assertEqual(add(0, 0), 0)

if __name__ == "__main__":
    unittest.main()

Then run: python test_adder.py
The tests MUST fail because of the bug. Do not change adder.py to addition.
When finished, reply with the exact line: TURN1_BUG_CONFIRMED
"""

TURN2_PROMPT = """adder.py still has an intentional bug: add(a, b) returns a - b.
Fix adder.py so add(a, b) returns a + b. Do not weaken the tests.
Run: python test_adder.py
All tests must pass.
When finished, reply with the exact line: TURN2_TESTS_PASSED
"""


def artifacts_dir() -> Path:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS


def basic_auth() -> tuple[str, str]:
    user = os.environ.get("SBX_BASIC_USER") or os.environ.get("SBX_API_USER") or ""
    password = os.environ.get("SBX_BASIC_PASS") or os.environ.get("SBX_API_PASSWORD") or ""
    if not user or not password:
        raise RuntimeError("SBX_BASIC_USER / SBX_BASIC_PASS are required for e2e_modal")
    return user, password


def discover_control_url() -> str:
    explicit = (os.environ.get("SBX_CONTROL_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    marker = artifacts_dir() / "control_url.txt"
    if marker.is_file():
        saved = marker.read_text(encoding="utf-8").strip().rstrip("/")
        if saved:
            return saved
    return DEFAULT_CONTROL_URL.rstrip("/")


def save_control_url(url: str) -> None:
    path = artifacts_dir() / "control_url.txt"
    path.write_text(url.rstrip("/") + "\n", encoding="utf-8")


def client_for(base: str | None = None, timeout: float = 120.0) -> httpx.Client:
    url = (base or discover_control_url()).rstrip("/")
    return httpx.Client(base_url=url, auth=basic_auth(), timeout=timeout, follow_redirects=True)


def wait_session(
    client: httpx.Client,
    sid: str,
    *,
    status: str | None = None,
    min_turns: int | None = None,
    timeout: float = CREATE_TIMEOUT_S,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        resp = client.get(f"/api/sessions/{sid}")
        resp.raise_for_status()
        last = resp.json()
        ok = True
        if status is not None:
            ok = ok and last["status"] == status
        if min_turns is not None:
            ok = ok and int(last.get("turns") or 0) >= min_turns
        if ok:
            return last
        time.sleep(POLL_S)
    raise AssertionError(f"timeout waiting for session {sid}: {last}")


def close_session(client: httpx.Client, sid: str) -> dict[str, Any] | None:
    try:
        client.post(f"/api/sessions/{sid}/stop")
    except httpx.HTTPError:
        pass
    try:
        resp = client.delete(f"/api/sessions/{sid}")
    except httpx.HTTPError:
        return None
    if resp.status_code == 200:
        return resp.json()
    return None


def close_active_sessions(client: httpx.Client) -> None:
    try:
        resp = client.get("/api/sessions")
        resp.raise_for_status()
    except httpx.HTTPError:
        return
    for sess in resp.json():
        if sess.get("status") in {"creating", "idle", "running"}:
            close_session(client, sess["id"])


def create_session(
    client: httpx.Client, title: str, model: str = "gpt-5.6-luna"
) -> tuple[str, float]:
    t0 = time.monotonic()
    resp = client.post("/api/sessions", json={"title": title, "model": model})
    assert resp.status_code == 201, resp.text
    sid = resp.json()["session_id"]
    rec = wait_session(client, sid, status="idle", timeout=CREATE_TIMEOUT_S)
    cold = time.monotonic() - t0
    assert rec["status"] == "idle"
    return sid, cold


def post_turn(
    client: httpx.Client, sid: str, text: str, *, timeout: float = TURN_TIMEOUT_S
) -> tuple[dict[str, Any], float]:
    before = client.get(f"/api/sessions/{sid}")
    before.raise_for_status()
    n = int(before.json().get("turns") or 0)
    t0 = time.monotonic()
    resp = client.post(f"/api/sessions/{sid}/messages", json={"text": text})
    assert resp.status_code == 202, resp.text
    rec = wait_session(client, sid, status="idle", min_turns=n + 1, timeout=timeout)
    return rec, time.monotonic() - t0


def usage_numbers(rec: dict[str, Any]) -> dict[str, int]:
    usage = rec.get("usage") or {}
    out: dict[str, int] = {}
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "cache_write_input_tokens",
        "reasoning_output_tokens",
    ):
        if key in usage and usage[key] is not None:
            out[key] = int(usage[key])
    return out


def write_json(name: str, payload: dict[str, Any]) -> Path:
    path = artifacts_dir() / name
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def list_sandboxes(session_id: str | None = None, owner: str | None = None) -> list[Any]:
    from control.backends.modal import ModalBackend

    backend = ModalBackend()
    tags: dict[str, str] = {}
    if session_id:
        tags["session_id"] = session_id
    if owner:
        tags["owner"] = owner
    return backend.list(tags=tags or None)


def sandbox_read(session_id: str, relative: str) -> str | None:
    from control.backends.modal import ModalBackend
    from control.sandbox_io import read_text

    backend = ModalBackend()
    handles = backend.list(tags={"session_id": session_id})
    if not handles:
        return None
    return read_text(backend, handles[0], relative)


def sandbox_ls(session_id: str, path: str = "/opt/sbx/runtime") -> str:
    from control.backends.modal import ModalBackend
    from control.sandbox_io import sandbox_env

    backend = ModalBackend()
    handles = backend.list(tags={"session_id": session_id})
    if not handles:
        raise AssertionError(f"no sandbox for session {session_id}")
    proc = backend.exec(handles[0], ["ls", "-la", path], env=sandbox_env(handles[0]))
    chunks = list(proc.stdout)
    code = proc.wait()
    text = "\n".join(chunks)
    if code != 0:
        raise AssertionError(f"ls {path} exited {code}: {text[-400:]}")
    return text


def assistant_texts(rec: dict[str, Any]) -> list[str]:
    return [m.get("text") or "" for m in rec.get("messages") or [] if m.get("role") == "assistant"]


def leak_reason(blob: str) -> str | None:
    """Return a label if secrets appear. Never include matched secret text."""
    checks = (
        ("CODEX_AUTH_JSON", os.environ.get("CODEX_AUTH_JSON")),
        ("MODAL_TOKEN_SECRET", os.environ.get("MODAL_TOKEN_SECRET")),
        ("MODAL_TOKEN_ID", os.environ.get("MODAL_TOKEN_ID")),
        ("SBX_BASIC_PASS", os.environ.get("SBX_BASIC_PASS")),
    )
    for name, value in checks:
        if value and len(value) >= 8 and value in blob:
            return name
    if JWT_RE.search(blob):
        return "jwt"
    if SK_RE.search(blob):
        return "sk-token"
    lower = blob.lower()
    if "bearer eyj" in lower:
        return "bearer-jwt"
    return None


def scan_path_for_leaks(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    text = data.decode("utf-8", "replace")
    return leak_reason(text)


def modal_app_logs(app: str = "sbx-control", tail: int = 400) -> str:
    env = os.environ.copy()
    env.setdefault("MODAL_PROFILE", "sorenlab2026")
    proc = subprocess.run(
        ["uv", "run", "modal", "app", "logs", app, "--tail", str(tail)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return (proc.stdout or "") + (proc.stderr or "")
