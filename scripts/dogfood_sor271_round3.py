#!/usr/bin/env python3
"""SOR-271 round-3 live dogfood: real uvicorn control plane + real HTTP.

Replays the API_PERF_GATE failures end to end against a live server:

* CAPACITY — ``open_session`` counts only genuinely live bound agents +
  fresh in-flight creates. An orphaned live sandbox (leaked abandoned
  bind), a terminal record's surviving handle, and a wedged ``creating``
  record older than the create grace all sit on the plane while new
  sessions still bind; once two real agents are bound the cap still
  trips with ``concurrency_limit``.
* REAPER — the same objects drive an in-process sweep identical to the
  cron's ``reap()``: a throwing ``on_action`` (the production callback
  settles orphaned runs via the remote run ledger) no longer aborts the
  records loop or skips the orphan pass, and a throwing
  ``reconcile_turns`` store listing degrades to an empty settle instead
  of killing the tick before reaping.

Usage: ``uv run python scripts/dogfood_sor271_round3.py`` — exits non-zero
on any failed check and prints a transcript safe to paste into a PR.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from control.app import create_app
from control.backend import LocalProcessBackend, SandboxSpec
from control.ports import Account
from control.reaper import reap
from control.store import InMemoryStore, SessionRecord, empty_usage
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


def create_session(http: httpx.Client, base: str, auth: dict[str, str], prompt: str) -> Any:
    return http.post(
        f"{base}/v2/sessions",
        json={"prompt": prompt, "execution": {"provider": "codex"}},
        headers=auth,
    )


def phantom_record(
    session_id: str,
    status: str,
    *,
    owner: str,
    handle_id: str | None = None,
    created: datetime | None = None,
) -> SessionRecord:
    at = created or datetime.now(UTC)
    return SessionRecord(
        id=session_id,
        title="phantom",
        status=status,
        created_at=at,
        updated_at=at,
        model="m",
        turns=0,
        usage=empty_usage(),
        messages=[],
        owner=owner,
        sandbox_id=handle_id,
        sandbox_root="/tmp/phantom" if handle_id else None,
        sandbox_tags={"session_id": session_id, "owner": owner} if handle_id else {},
        last_activity_at=at,
    )


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
    key, token = keys.create(label="dogfood-r3", scopes=("agents",))
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

    plane = app.state.plane
    print("SOR-271 round-3 dogfood — capacity accounting + reaper ownership")
    try:
        # -- 1. One genuinely bound agent -----------------------------------
        plane.max_concurrent = 2
        resp = create_session(http, base, auth, "Create a.txt.")
        check(
            "session A dispatch accepted",
            resp.status_code in (200, 201),
            f"got {resp.status_code}",
        )
        sid_a = http.get(f"{base}/v2/sessions", headers=auth).json()["sessions"][0]["id"]
        wait_status(http, base, auth, sid_a, "finished")
        bound = {r.id: r for r in store.list_all() if r.owner == key.id}
        check(
            "session A bound to a live sandbox (1 occupied slot)",
            any(r.sandbox_id and backend.poll(r.handle()).alive for r in bound.values()),
        )
        a_agent = next(r.id for r in bound.values() if r.sandbox_id)

        # -- 2. Phantom occupants: orphan sandbox, terminal-bound sandbox,
        #       wedged creating record — none may consume a slot. ----------
        orphan = backend.create(SandboxSpec(tags={"session_id": "ghost", "owner": key.id}))
        dead = backend.create(SandboxSpec(tags={"session_id": "dead", "owner": key.id}))
        store.put(phantom_record("dead", "lost", owner=key.id, handle_id=dead.id))
        store.put(
            phantom_record(
                "wedged",
                "creating",
                owner=key.id,
                created=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        live_handles = len(backend.list(tags={"owner": key.id}))
        check(
            "phantoms staged: 3 live sandboxes, 1 genuinely bound",
            live_handles == 3,
            f"live={live_handles}",
        )
        resp = create_session(http, base, auth, "Create b.txt.")
        check(
            "bind succeeds despite phantoms (old math: live=3 >= cap=2)",
            resp.status_code in (200, 201),
            f"got {resp.status_code} {resp.json().get('error')}",
        )
        sid_b = next(
            s["id"]
            for s in http.get(f"{base}/v2/sessions", headers=auth).json()["sessions"]
            if s["id"] != sid_a
        )
        wait_status(http, base, auth, sid_b, "finished")
        check("session B bound alongside A (2 genuinely live bound agents)", True)

        # -- 3. The cap still trips on real bound agents ---------------------
        resp = create_session(http, base, auth, "Create c.txt.")
        check(
            "cap=2 still trips with retryable concurrency_limit",
            resp.status_code == 429
            and (resp.json().get("error") or {}).get("code") == "concurrency_limit",
            f"got {resp.status_code} {resp.json().get('error')}",
        )

        # -- 4. Reaper tick: a throwing on_action never aborts the sweep ----
        stale = store.get(a_agent)
        assert stale is not None
        stale.last_activity_at = datetime.now(UTC) - timedelta(seconds=3600)
        stale.updated_at = stale.last_activity_at
        store.put(stale)

        def boom(action) -> None:
            raise RuntimeError("settle_orphaned_runs: run ledger down")

        actions = reap(
            store,
            backend,
            datetime.now(UTC),
            idle_timeout_s=300,
            checkpoints=None,
            on_action=boom,
        )
        kinds = [a.kind for a in actions]
        rec_a = store.get(a_agent)
        check(
            "idle-expired bound agent reaped despite throwing callback",
            rec_a is not None and rec_a.status == "timed_out",
            f"status={rec_a.status if rec_a else None}",
        )
        check(
            "orphan sandbox reclaimed in the same sweep",
            backend.poll(orphan).alive is False,
        )
        wedged = store.get("wedged")
        check(
            "wedged creating record settled to lost",
            wedged is not None and wedged.status == "lost",
            f"status={wedged.status if wedged else None}",
        )
        check(
            "callback failures surfaced as reap_error, sweep continued",
            "reap_error" in kinds and "orphan_terminate" in kinds,
            str(kinds),
        )

        # -- 5. Cron invocation path: reconcile failure cannot starve reap --
        original = plane.store.list_all
        plane.store.list_all = lambda: (_ for _ in ()).throw(  # type: ignore[assignment]
            RuntimeError("dict unreachable")
        )
        try:
            settled = plane.reconcile_turns()
            check(
                "reconcile_turns degrades to empty settle, reap still reached",
                settled == [],
            )
        finally:
            plane.store.list_all = original  # type: ignore[assignment]
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
