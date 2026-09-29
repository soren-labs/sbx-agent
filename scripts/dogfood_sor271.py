#!/usr/bin/env python3
"""SOR-271 live dogfood: real uvicorn control plane + real HTTP.

Replays the API_PERF_GATE failures end to end against a live server:

* LIFECYCLE — a session failing pre-bind (``concurrency_limit``) is
  retryable: ``/retry`` re-drives the original dispatch on the same
  session id — failing again while the cap holds (as the real dispatch
  error, NOT a permanent ``session_not_runnable``), and binding an agent
  once capacity returns. Non-retryable dispatch failures answer
  ``task_not_retryable``; terminal agent-less rows answer
  ``task_not_retryable``.
* READ-MODEL — ``GET /v2/sessions`` converges to the same terminal status
  as the detail view at every step (the stale-``queued`` divergence the
  gate observed on the ModalDictTaskStore index is fixed by point-reading
  agent-less summaries; InMemory exercises the same contract).

Usage: ``uv run python scripts/dogfood_sor271.py`` — exits non-zero on any
failed check and prints a transcript safe to paste into a PR.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from control.app import create_app
from control.backend import LocalProcessBackend
from control.ports import Account
from control.store import InMemoryStore
from tests.fakes.fake_ports import (
    InMemoryAccountRegistry,
    InMemoryApiKeyStore,
    InMemoryScheduler,
)

REPO = Path(__file__).resolve().parents[1]
STUB_RUNNER = REPO / "tests" / "fakes" / "stub_runner.py"

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_status(http: httpx.Client, base: str, auth: dict[str, str], sid: str, want: str) -> dict:
    deadline = time.monotonic() + 30
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = http.get(f"{base}/v2/sessions/{sid}", headers=auth).json()
        if last["session"]["status"] == want:
            return last
        time.sleep(0.2)
    raise AssertionError(f"session {sid} never reached {want}: {last['session']}")


def list_status(http: httpx.Client, base: str, auth: dict[str, str], sid: str) -> str:
    body = http.get(f"{base}/v2/sessions", headers=auth).json()
    return next(s["status"] for s in body["sessions"] if s["id"] == sid)


def main() -> int:
    os.environ.setdefault("SBX_PROVIDERS", "codex,antigravity,grok,opencode,devin")
    backend = LocalProcessBackend()
    store = InMemoryStore()
    app = create_app(
        backend=backend,
        store=store,
        runner_cmd=[sys.executable, str(STUB_RUNNER)],
        keepalive_s=0.2,
    )
    registry = InMemoryAccountRegistry()
    app.state.account_registry = registry
    app.state.scheduler = InMemoryScheduler(registry)
    keys = InMemoryApiKeyStore()
    app.state.api_key_store = keys
    registry.put(
        Account(
            id="acct-codex-1",
            provider="codex",
            label="codex account",
            models=("gpt-5.6-luna", "gpt-5.3-codex"),
            created_at="2026-09-29T00:00:00+00:00",
        )
    )
    registry.put_credential_blob(
        "acct-codex-1", {"provider": "codex", "files": {".codex/auth.json": "{}"}}
    )
    _, token = keys.create(label="dogfood", scopes=("agents",))
    auth = {"Authorization": f"Bearer {token}"}

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    http = httpx.Client(timeout=30)
    for _ in range(100):
        try:
            http.get(f"{base}/health")
            break
        except httpx.TransportError:
            time.sleep(0.1)

    print("SOR-271 dogfood — pre-bind retry + list convergence")
    try:
        # -- 1. Pre-bind failure: global cap forces concurrency_limit ----------
        app.state.plane.max_concurrent = 0
        resp = http.post(
            f"{base}/v2/sessions",
            json={"prompt": "Create hello.txt.", "execution": {"provider": "codex"}},
            headers=auth,
        )
        check(
            "create under cap surfaces retryable concurrency_limit",
            resp.status_code == 429,
            f"got {resp.status_code}",
        )
        sid = http.get(f"{base}/v2/sessions", headers=auth).json()["sessions"][0]["id"]
        detail = wait_status(http, base, auth, sid, "failed")
        err = detail["session"]["error"] or {}
        check(
            "pre-bind failure is terminal+retryable on detail",
            err.get("code") == "concurrency_limit" and err.get("retryable") is True,
            str(err),
        )
        check(
            "list converged to failed (never stuck queued)",
            list_status(http, base, auth, sid) == "failed",
        )

        # -- 2. Retry while still capped: re-drive, fail again, stay retryable -
        resp = http.post(f"{base}/v2/sessions/{sid}/retry", json={}, headers=auth)
        err2 = resp.json().get("error") or {}
        check(
            "retry re-drives dispatch (429 concurrency_limit, not 409 not_runnable)",
            resp.status_code == 429 and err2.get("code") == "concurrency_limit",
            f"got {resp.status_code} {err2}",
        )
        wait_status(http, base, auth, sid, "failed")
        check(
            "list still converged after second dispatch failure",
            list_status(http, base, auth, sid) == "failed",
        )

        # -- 3. Capacity restored: retry binds an agent and completes ----------
        app.state.plane.max_concurrent = 64
        resp = http.post(f"{base}/v2/sessions/{sid}/retry", json={}, headers=auth)
        check("restored retry accepted", resp.status_code == 200, resp.text[:120])
        check(
            "re-drive created run-1 on the same session",
            (resp.json().get("run") or {}).get("n") == 1,
            str(resp.json().get("run")),
        )
        done = wait_status(http, base, auth, sid, "finished")
        check("session finished, error cleared", done["session"]["error"] is None)
        check("list converged to finished", list_status(http, base, auth, sid) == "finished")

        record = app.state.task_store.get(sid)
        reasons = [t["reason"] for t in record.transitions]
        check(
            "attempt history preserved (dispatch_failed x2 + retry_dispatch x2)",
            reasons.count("dispatch_failed") == 2 and reasons.count("retry_dispatch") == 2,
            str(reasons),
        )
        check(
            "agent bound on the same session id",
            record.agent_id is not None,
            record.agent_id or "",
        )

        # -- 4. Non-retryable pre-bind failure → task_not_retryable ---------
        resp = http.post(
            f"{base}/v2/sessions",
            json={
                "prompt": "x",
                "execution": {"provider": "codex"},
                "repository": {"repo": "https://example.invalid/x/y", "ref": "bad ref!!"},
            },
            headers=auth,
        )
        sessions = http.get(f"{base}/v2/sessions", headers=auth).json()["sessions"]
        sid2 = next(s["id"] for s in sessions if s["id"] != sid)
        failed2 = wait_status(http, base, auth, sid2, "failed")
        check(
            "workspace resolution failure is non-retryable",
            (failed2["session"]["error"] or {}).get("retryable") is False,
            str(failed2["session"]["error"]),
        )
        resp = http.post(f"{base}/v2/sessions/{sid2}/retry", json={}, headers=auth)
        err3 = resp.json().get("error") or {}
        check(
            "non-retryable pre-bind failure → 409 task_not_retryable",
            resp.status_code == 409 and err3.get("code") == "task_not_retryable",
            f"got {resp.status_code} {err3}",
        )

        # -- 5. Terminal agent-less row → session_not_runnable -----------------
        record2 = app.state.task_store.get(sid2)
        record2.status = "cancelled"
        record2.transitions.append({"status": "cancelled", "reason": "cancelled", "at": "t"})
        app.state.task_store.put(record2)
        resp = http.post(f"{base}/v2/sessions/{sid2}/retry", json={}, headers=auth)
        err4 = resp.json().get("error") or {}
        check(
            "terminal agent-less session → 409 task_not_retryable",
            resp.status_code == 409 and err4.get("code") == "task_not_retryable",
            f"got {resp.status_code} {err4}",
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        for handle in list(backend.list()):
            try:
                backend.terminate(handle)
            except Exception:
                pass

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
