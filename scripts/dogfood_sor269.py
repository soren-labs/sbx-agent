#!/usr/bin/env python3
"""SOR-269 live dogfood: real uvicorn control plane + real HTTP/SSE + stub runner.

Replays the SOR-260 gate failures end to end against a live server:

* B6 — ``auth_invalid`` → ``retry`` → ``success``: the Session-level
  ``error`` tracks the CURRENT attempt on every read surface (create/retry
  response, GET detail, GET list, SSE ``session.status`` frames), while the
  failed attempt stays traceable on its own run row.
* B7 — follow-up messages that change the workdir materialize one Changes
  row per logical revision: ordered ``n``, unique ``id``, no duplicates.

Usage: ``uv run python scripts/dogfood_sor269.py`` — exits non-zero on any
failed check and prints a transcript safe to paste into a PR.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from control.app import create_app
from control.backend import LocalProcessBackend
from control.ports import Account
from control.revisions import Revision, revision_to_dict
from control.store import InMemoryStore
from tests.fakes.fake_ports import (
    InMemoryAccountRegistry,
    InMemoryApiKeyStore,
    InMemoryScheduler,
)

REPO = Path(__file__).resolve().parents[1]
STUB_RUNNER = REPO / "tests" / "fakes" / "stub_runner.py"
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "sbx-dogfood",
    "GIT_AUTHOR_EMAIL": "sbx-dogfood@localhost",
    "GIT_COMMITTER_NAME": "sbx-dogfood",
    "GIT_COMMITTER_EMAIL": "sbx-dogfood@localhost",
}

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")


def host_git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


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


def sse_status_frames(
    http: httpx.Client, base: str, auth: dict[str, str], sid: str, want: str
) -> list[dict[str, Any]]:
    """Read SSE frames until a ``session.status`` frame reports ``want``."""
    frames: list[dict[str, Any]] = []
    deadline = time.monotonic() + 15
    with http.stream("GET", f"{base}/v2/sessions/{sid}/events", headers=auth, timeout=None) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if time.monotonic() > deadline:
                break
            if not line.startswith("data:"):
                continue
            payload = json.loads(line[5:].strip())
            if payload.get("type") != "session.status":
                continue
            frames.append(payload)
            if payload.get("status") == want:
                return frames
    return frames


def make_origin(root: Path) -> tuple[Path, str]:
    repo = root / "origin"
    repo.mkdir()
    host_git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    host_git(repo, "add", "-A")
    host_git(repo, "commit", "-qm", "A")
    return repo, host_git(repo, "rev-parse", "HEAD")


def main() -> int:
    if shutil.which("git") is None:
        print("git binary unavailable — cannot dogfood revisions")
        return 2
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
    # Auth material so the seeded account passes the task auth filter, plus
    # head-room for concurrent sessions (mirrors the credentialed fixture).
    registry.put_credential_blob(
        "acct-codex-1", {"provider": "codex", "files": {".codex/auth.json": "{}"}}
    )
    app.state.plane.max_concurrent = 64
    _, token = keys.create(label="dogfood", scopes=("agents",))
    auth = {"Authorization": f"Bearer {token}"}

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    http = httpx.Client(timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            http.get(f"{base}/v1/me", timeout=1)
            break
        except httpx.TransportError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        print("server did not start")
        return 2
    print(f"live control plane on {base}")

    tmp = Path(tempfile.mkdtemp(prefix="sbx-dogfood-"))
    try:
        # ---------- B6: auth_invalid -> retry -> success ----------
        print("B6: auth_invalid attempt, then retry to success")
        os.environ["FAKE_CODEX_SCENARIO"] = "auth_invalid"
        resp = http.post(
            f"{base}/v2/sessions",
            json={"prompt": "first turn", "execution": {"provider": "codex"}},
            headers=auth,
        )
        assert resp.status_code == 201, resp.text
        sid = resp.json()["session"]["id"]

        failed = wait_status(http, base, auth, sid, "failed")
        check(
            "failed attempt exposes auth_invalid at session level",
            failed["session"].get("error", {}).get("code") == "auth_invalid",
            json.dumps(failed["session"].get("error")),
        )
        check(
            "same error on the attempt's own run row",
            failed["runs"][0].get("error", {}).get("code") == "auth_invalid",
        )
        frames = sse_status_frames(http, base, auth, sid, "failed")
        check(
            "SSE replay agrees: session.status=failed",
            bool(frames) and frames[-1]["status"] == "failed",
            json.dumps(frames[-1]) if frames else "no frames",
        )

        os.environ["FAKE_CODEX_SCENARIO"] = "success"
        resp = http.post(f"{base}/v2/sessions/{sid}/retry", json={}, headers=auth)
        assert resp.status_code == 200, resp.text
        check(
            "retry response already clears the stale error",
            resp.json()["session"]["error"] is None,
            f"status={resp.json()['session']['status']}",
        )

        done = wait_status(http, base, auth, sid, "finished")
        check(
            "finished session carries no stale error",
            done["session"]["error"] is None and done["session"]["status"] == "finished",
        )
        check(
            "history stays traceable: run-1 failed/auth_invalid, run-2 clean",
            done["run_count"] == 2
            and done["runs"][0]["status"] == "failed"
            and done["runs"][0]["error"]["code"] == "auth_invalid"
            and done["runs"][1].get("error") is None,
            "runs="
            + json.dumps([(r["status"], (r.get("error") or {}).get("code")) for r in done["runs"]]),
        )
        rows = http.get(f"{base}/v2/sessions", headers=auth).json()["sessions"]
        row = next(r for r in rows if r["id"] == sid)
        check(
            "post-refresh list read agrees: finished + error null",
            row["status"] == "finished" and row["error"] is None,
            json.dumps({k: row[k] for k in ("status", "error")}),
        )
        frames = sse_status_frames(http, base, auth, sid, "finished")
        check(
            "SSE reconnect never resurrects the stale error",
            bool(frames) and frames[-1]["status"] == "finished",
            json.dumps(frames[-1]) if frames else "no frames",
        )

        # ---------- B7: multi-follow-up revisions ----------
        print("B7: two code-changing follow-ups -> two ordered, unique revisions")
        repo, _head = make_origin(tmp)
        resp = http.post(
            f"{base}/v2/sessions",
            json={
                "prompt": "Create hello.txt.",
                "execution": {"provider": "codex"},
                "repository": {"repo": f"file://{repo}"},
            },
            headers=auth,
        )
        assert resp.status_code == 201, resp.text
        sid2 = resp.json()["session"]["id"]
        wait_status(http, base, auth, sid2, "finished")

        rec = app.state.task_store.get(sid2)
        assert rec is not None and rec.agent_id
        agent_id = rec.agent_id
        arec = store.get(agent_id)
        assert arec is not None and arec.handle() is not None
        wd = Path(arec.handle().root) / "repo"

        for name in ("b.txt", "c.txt"):
            (wd / name).write_text(f"{name}\n", encoding="utf-8")
            host_git(wd, "add", "-A")
            host_git(wd, "commit", "-qm", f"add {name}")
            resp = http.post(
                f"{base}/v2/sessions/{sid2}/messages", json={"prompt": "more work"}, headers=auth
            )
            assert resp.status_code == 202, resp.text

        want_ns = [1, 2]
        rows = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            rows = http.get(f"{base}/v2/sessions/{sid2}/changes", headers=auth).json()["revisions"]
            if [r["n"] for r in rows] == want_ns:
                break
            time.sleep(0.2)
        check(
            "one row per logical revision, ordered, no duplicates",
            [r["n"] for r in rows] == want_ns and len(rows) == 2,
            json.dumps([(r["n"], (r["head_sha"] or "")[:8]) for r in rows]),
        )

        # The pre-fix production shape: a raced duplicate stored row for one
        # run must still collapse to a single Changes row on the wire.
        rev_store = app.state.revision_store
        stored = rev_store.list_revisions(agent_id)
        rev_store.put_revision(
            Revision(**{**revision_to_dict(stored[0]), "revision_id": "rev-duprow01"})
        )
        again = http.get(f"{base}/v2/sessions/{sid2}/changes", headers=auth).json()["revisions"]
        check(
            "a raced duplicate stored row collapses to one Changes row",
            [r["n"] for r in again] == want_ns and len(again) == 2,
            f"{len(stored)} stored + 1 injected -> {len(again)} served",
        )

        ok = all(passed for _, passed, _ in CHECKS)
        print("RESULT:", "ALL CHECKS PASSED" if ok else "FAILURES PRESENT")
        return 0 if ok else 1
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        for handle in list(backend.list()):
            backend.terminate(handle)


if __name__ == "__main__":
    sys.exit(main())
