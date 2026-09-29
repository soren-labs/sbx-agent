#!/usr/bin/env python3
"""SOR-270 live dogfood: ad-hoc deliver lands on ``session.delivery``.

Real uvicorn control plane + real HTTP + real git push to a ``file://``
origin. Replays the SOR-270 UI-gate failure end to end: a session created
WITHOUT a delivery policy delivers ad-hoc — ``session.delivery`` must
project the branch push and the pull request identically on the deliver
response, GET detail, GET list, and the changes facade; a follow-up +
redeliver must update the same PR in place without duplicating revisions.

The GitHub leg is the control-plane seam only (``repo_slug`` + the remote
client are faked in-process); the wire projections, revision materialization,
and branch push all run for real.

Usage: ``uv run python scripts/dogfood_sor270.py`` — exits non-zero on any
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
from control import revisions as revisions_mod
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


def delivery_views(http: httpx.Client, base: str, auth: dict[str, str], sid: str) -> dict[str, Any]:
    """``session.delivery`` on the three read surfaces."""
    detail = http.get(f"{base}/v2/sessions/{sid}", headers=auth).json()["session"]
    listed = next(
        s
        for s in http.get(f"{base}/v2/sessions", headers=auth).json()["sessions"]
        if s["id"] == sid
    )
    changes = http.get(f"{base}/v2/sessions/{sid}/changes", headers=auth).json()["session"]
    return {
        "detail": detail["delivery"],
        "list": listed["delivery"],
        "changes": changes["delivery"],
    }


def make_origin(root: Path) -> tuple[Path, str]:
    repo = root / "origin"
    repo.mkdir()
    host_git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    host_git(repo, "add", "-A")
    host_git(repo, "commit", "-qm", "A")
    return repo, host_git(repo, "rev-parse", "HEAD")


class FakeRemote:
    """Control-plane GitHub seam for the dogfood run (no network)."""

    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []

    def find_pull(self, slug: str, head_branch: str) -> dict[str, Any] | None:
        return None

    def get_pull(self, slug: str, number: int) -> dict[str, Any]:
        return {
            "number": number,
            "head": {"sha": "0" * 40, "ref": "session/work"},
            "state": "open",
            "draft": False,
        }

    def create_pull(self, slug: str, **kw: Any) -> dict[str, Any]:
        self.create_calls.append(dict(kw))
        return {
            "number": 7,
            "html_url": f"https://github.com/{slug}/pull/7",
            "state": "open",
            "draft": bool(kw.get("draft")),
        }

    def update_pull(self, slug: str, number: int, **kw: Any) -> dict[str, Any]:
        self.update_calls.append(dict(kw))
        return {
            "number": number,
            "html_url": f"https://github.com/{slug}/pull/{number}",
            "state": "open",
            "base": {"ref": "main"},
        }


def main() -> int:
    if shutil.which("git") is None:
        print("git binary unavailable — cannot dogfood delivery")
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
        # ---------- a session with NO delivery policy, one real revision ----------
        print("Setup: repo session, one code-changing follow-up -> revision 1")
        os.environ["FAKE_CODEX_SCENARIO"] = "success"
        repo, _base = make_origin(tmp)
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
        sid = resp.json()["session"]["id"]
        wait_status(http, base, auth, sid, "finished")

        rec = app.state.task_store.get(sid)
        assert rec is not None and rec.agent_id
        agent_id = rec.agent_id
        arec = store.get(agent_id)
        assert arec is not None and arec.handle() is not None
        wd = Path(arec.handle().root) / "repo"

        (wd / "b.txt").write_text("two\n", encoding="utf-8")
        host_git(wd, "add", "-A")
        host_git(wd, "commit", "-qm", "add b.txt")
        head = host_git(wd, "rev-parse", "HEAD")
        resp = http.post(
            f"{base}/v2/sessions/{sid}/messages",
            json={"prompt": "more work"},
            headers=auth,
        )
        assert resp.status_code == 202, resp.text
        wait_status(http, base, auth, sid, "finished")

        rows = http.get(f"{base}/v2/sessions/{sid}/changes", headers=auth).json()["revisions"]
        assert [r["n"] for r in rows] == [1], rows
        check(
            "pre-deliver: session.delivery is null (nothing delivered yet)",
            delivery_views(http, base, auth, sid)["detail"] is None,
        )

        # ---------- ad-hoc branch deliver (real push to file:// origin) ----------
        print("Ad-hoc deliver: branch push must land on session.delivery")
        resp = http.post(
            f"{base}/v2/sessions/{sid}/deliver",
            json={"branch": "session/work"},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        delivery = resp.json()["session"]["delivery"]
        check(
            "deliver response: session.delivery non-null, branch delivered",
            delivery is not None
            and delivery["required"] is False
            and delivery["status"] == "delivered"
            and delivery["branch"] == "session/work"
            and delivery["pushed_head_sha"] == head,
            json.dumps(delivery),
        )
        pushed = host_git(repo, "rev-parse", "session/work")
        check("the durable push landed on the file:// origin", pushed == head, pushed[:8])
        views = delivery_views(http, base, auth, sid)
        check(
            "GET detail / list / changes all agree post-refresh",
            views["detail"] == delivery
            and views["list"] == delivery
            and views["changes"] == delivery,
        )

        # ---------- ad-hoc PR deliver (faked control-plane GitHub seam) ----------
        print("Ad-hoc deliver with pull_request: the PR must surface")
        revisions_mod.github.repo_slug = lambda repo: "acme/widgets"  # noqa: E731
        remote = FakeRemote()
        app.state.revisions._remote = remote
        resp = http.post(
            f"{base}/v2/sessions/{sid}/deliver",
            json={"pull_request": {"title": "ship it"}},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        delivery = resp.json()["session"]["delivery"]
        pr = (delivery or {}).get("pull_request") or {}
        check(
            "deliver response: session.delivery.pull_request carries url+number",
            pr.get("number") == 7
            and pr.get("url") == "https://github.com/acme/widgets/pull/7"
            and pr.get("state") == "open"
            and delivery["status"] == "delivered",
            json.dumps(pr),
        )
        views = delivery_views(http, base, auth, sid)
        check(
            "detail / list / changes project the same PR",
            all((v or {}).get("pull_request", {}).get("number") == 7 for v in views.values())
            and views["detail"] == delivery
            and views["list"] == delivery
            and views["changes"] == delivery,
        )

        # ---------- follow-up + redeliver: same PR updated, no dup rows ----------
        print("Follow-up + redeliver: the same PR is updated in place")
        (wd / "c.txt").write_text("three\n", encoding="utf-8")
        host_git(wd, "add", "-A")
        host_git(wd, "commit", "-qm", "add c.txt")
        host_git(wd, "rev-parse", "HEAD")
        resp = http.post(
            f"{base}/v2/sessions/{sid}/messages",
            json={"prompt": "even more work"},
            headers=auth,
        )
        assert resp.status_code == 202, resp.text
        wait_status(http, base, auth, sid, "finished")

        resp = http.post(
            f"{base}/v2/sessions/{sid}/deliver",
            json={"pull_request": {"title": "ship it v2"}},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        delivery = resp.json()["session"]["delivery"]
        pr = (delivery or {}).get("pull_request") or {}
        check(
            "redeliver updates the SAME PR: number kept, head advanced",
            pr.get("number") == 7 and pr.get("head_sha") == delivery["pushed_head_sha"],
            json.dumps({"number": pr.get("number"), "head": (pr.get("head_sha") or "")[:8]}),
        )
        check(
            "no new upstream PR was created (update_pull, not create_pull)",
            len(remote.create_calls) == 1 and len(remote.update_calls) == 1,
            f"create={len(remote.create_calls)} update={len(remote.update_calls)}",
        )
        rows = http.get(f"{base}/v2/sessions/{sid}/changes", headers=auth).json()["revisions"]
        check(
            "B7 holds: redeliver created no extra revision rows",
            [r["n"] for r in rows] == [1, 2],
            json.dumps([r["n"] for r in rows]),
        )
        views = delivery_views(http, base, auth, sid)
        check(
            "post-refresh reads stay identical after redeliver",
            views["detail"] == delivery
            and views["list"] == delivery
            and views["changes"] == delivery,
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
