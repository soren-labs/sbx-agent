"""WP2-F item 6: slow scenario SSE keepalive + Last-Event-ID reconnect."""

from __future__ import annotations

import threading
import time

import httpx
from fastapi.testclient import TestClient
from tests.integration.cloud_free.conftest import AUTH
from tests.integration.cloud_free.helpers import start_server, stop_server, wait_session


def test_item6_slow_keepalive_and_last_event_id_reconnect(live_env, monkeypatch) -> None:
    """During fake_codex silence, SSE still keepalives; resume skips without dup/gap."""
    app, _backend, _store = live_env
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "2")
    server, thread, base = start_server(app)
    sid = ""
    try:
        with httpx.Client(base_url=base, timeout=20.0) as api:
            created = api.post("/api/sessions", json={"title": "slow"}, auth=AUTH)
            assert created.status_code == 201
            sid = created.json()["session_id"]
            wait_session(TestClient(app), sid, status="idle")

            first_ids: list[int] = []
            first_events: list[str] = []
            keepalives_after_thread = 0
            saw_thread = False
            next_event_after_thread = False

            def _post() -> None:
                posted = api.post(f"/api/sessions/{sid}/messages", json={"text": "slow"}, auth=AUTH)
                assert posted.status_code == 202, posted.text

            poster = threading.Thread(target=_post, daemon=True)
            with httpx.Client(base_url=base, timeout=20.0) as sse:
                poster.start()
                with sse.stream("GET", f"/api/sessions/{sid}/events", auth=AUTH) as resp:
                    assert resp.status_code == 200
                    assert "text/event-stream" in resp.headers["content-type"]
                    deadline = time.monotonic() + 8
                    for line in resp.iter_lines():
                        if line.startswith(": keepalive"):
                            if saw_thread and not next_event_after_thread:
                                keepalives_after_thread += 1
                                if keepalives_after_thread >= 2:
                                    break
                        elif line.startswith("id:"):
                            first_ids.append(int(line.split(":", 1)[1].strip()))
                            if saw_thread:
                                next_event_after_thread = True
                                break
                        elif line.startswith("event:"):
                            name = line.split(":", 1)[1].strip()
                            first_events.append(name)
                            if name == "thread.started":
                                saw_thread = True
                        if time.monotonic() > deadline:
                            break
                poster.join(timeout=5)

            assert saw_thread, first_events
            assert keepalives_after_thread >= 1, (
                f"expected keepalive during silence, got {keepalives_after_thread}; "
                f"events={first_events} ids={first_ids}"
            )
            assert first_ids
            last = first_ids[-1]
            assert min(first_ids) >= 1

            resume_ids: list[int] = []
            resume_events: list[str] = []
            with httpx.Client(base_url=base, timeout=20.0) as sse:
                with sse.stream(
                    "GET",
                    f"/api/sessions/{sid}/events",
                    auth=AUTH,
                    headers={"Last-Event-ID": str(last)},
                ) as resp:
                    deadline = time.monotonic() + 10
                    for line in resp.iter_lines():
                        if line.startswith("id:"):
                            resume_ids.append(int(line.split(":", 1)[1].strip()))
                        elif line.startswith("event:"):
                            resume_events.append(line.split(":", 1)[1].strip())
                            if "sbx.turn_finished" in resume_events:
                                break
                        if time.monotonic() > deadline:
                            break

            assert resume_ids, "Last-Event-ID resume should continue the stream"
            assert min(resume_ids) > last
            assert set(first_ids) & set(resume_ids) == set()
            combined = sorted(set(first_ids) | set(resume_ids))
            assert combined == list(range(combined[0], combined[-1] + 1))
            assert "sbx.turn_finished" in resume_events
            wait_session(TestClient(app), sid, status="idle", min_turns=1, timeout=10)
            api.delete(f"/api/sessions/{sid}", auth=AUTH)
    finally:
        stop_server(server, thread)
