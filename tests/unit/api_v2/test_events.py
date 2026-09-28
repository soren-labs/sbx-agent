"""SOR-256: ``GET /v2/sessions/{id}/events`` — normalized SSE.

Covers the event vocabulary, fan-out sharing (two subscribers on one feed),
and Last-Event-ID reconnect/replay.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest
from tests.unit.api_v2.conftest import (
    V1Env,
    create_session,
    iter_sse,
    wait_session,
)


def _collect(
    base: str,
    auth: dict[str, str],
    session_id: str,
    out: list,
    stop: threading.Event,
    last_event_id: str | None = None,
) -> None:
    """Stream the session's events into ``out`` until ``stop``."""
    headers = dict(auth)
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id
    try:
        with httpx.stream(
            "GET", f"{base}/v2/sessions/{session_id}/events", headers=headers, timeout=30
        ) as resp:
            for frame in iter_sse(resp):
                if stop.is_set():
                    return
                out.append(frame)
    except Exception:
        pass


def _wait_for(frames: list, predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(predicate(f) for f in frames):
            return True
        time.sleep(0.05)
    return False


def test_events_normalized_vocabulary(
    live_base: str,
    auth: dict[str, str],
    fast_events: V1Env,
    credentialed: V1Env,
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "4")
    session = create_session(client, auth)
    frames: list = []
    stop = threading.Event()
    thread = threading.Thread(
        target=_collect, args=(live_base, auth, session["id"], frames, stop), daemon=True
    )
    thread.start()
    try:
        # Immediate baseline status, then the run's progress events.
        assert _wait_for(frames, lambda f: f.get("event") == "session.status")
        assert _wait_for(
            frames,
            lambda f: f.get("event") == "session.status" and f["data"]["status"] == "running",
        )
        assert _wait_for(frames, lambda f: str(f.get("event", "")).startswith("activity."))
        done = wait_session(client, auth, session["id"], "finished")
        assert done["status"] == "finished"
        assert _wait_for(
            frames,
            lambda f: f.get("event") == "session.completed" and f["data"]["status"] == "finished",
        )
        assert _wait_for(frames, lambda f: f.get("event") == "usage.updated")
        types = {f.get("event") for f in frames}
        assert types <= {
            "session.status",
            "message.created",
            "activity.started",
            "activity.updated",
            "activity.completed",
            "usage.updated",
            "changes.updated",
            "delivery.updated",
            "session.completed",
            "session.failed",
        }
        # Session-scoped monotonic ids.
        seqs = [int(f["id"]) for f in frames if "id" in f]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    finally:
        stop.set()
        thread.join(timeout=5)


def test_follow_up_emits_message_created(
    live_base: str,
    auth: dict[str, str],
    fast_events: V1Env,
    credentialed: V1Env,
    client,
) -> None:
    session = create_session(client, auth)
    wait_session(client, auth, session["id"], "finished")
    frames: list = []
    stop = threading.Event()
    thread = threading.Thread(
        target=_collect, args=(live_base, auth, session["id"], frames, stop), daemon=True
    )
    thread.start()
    try:
        assert _wait_for(frames, lambda f: f.get("event") == "session.status")
        resp = client.post(
            f"/v2/sessions/{session['id']}/messages",
            json={"prompt": "follow up now"},
            headers=auth,
        )
        assert resp.status_code == 202, resp.text
        assert _wait_for(
            frames,
            lambda f: (
                f.get("event") == "message.created"
                and f["data"]["message"]["text"] == "follow up now"
            ),
        )
        wait_session(client, auth, session["id"], "finished")
        assert _wait_for(frames, lambda f: f.get("event") == "session.completed")
    finally:
        stop.set()
        thread.join(timeout=5)


def test_reconnect_replays_and_resumes(
    live_base: str,
    auth: dict[str, str],
    fast_events: V1Env,
    credentialed: V1Env,
    client,
) -> None:
    session = create_session(client, auth)
    wait_session(client, auth, session["id"], "finished")

    first: list = []
    stop1 = threading.Event()
    t1 = threading.Thread(
        target=_collect, args=(live_base, auth, session["id"], first, stop1), daemon=True
    )
    t1.start()
    try:
        assert _wait_for(first, lambda f: f.get("event") == "session.completed")
    finally:
        stop1.set()
        t1.join(timeout=5)
    assert len(first) >= 2
    # Resume from the first frame: the window after last_id must include
    # everything buffered since — tail activities can outrank session.completed.
    last_id = first[0]["id"]
    resumed: list = []
    stop2 = threading.Event()
    t2 = threading.Thread(
        target=_collect,
        args=(live_base, auth, session["id"], resumed, stop2, last_id),
        daemon=True,
    )
    t2.start()
    time.sleep(0.6)
    stop2.set()
    t2.join(timeout=5)
    assert resumed, "expected replay tail after last_event_id"
    assert all(int(f["id"]) > int(last_id) for f in resumed)
    assert any(f.get("event") == "session.completed" for f in resumed)

    # No Last-Event-ID: the retained window replays from its start
    # (practical reconnect — possibly a freshly normalized stream).
    replay: list = []
    stop3 = threading.Event()
    t3 = threading.Thread(
        target=_collect, args=(live_base, auth, session["id"], replay, stop3), daemon=True
    )
    t3.start()
    time.sleep(0.6)
    stop3.set()
    t3.join(timeout=5)
    assert len(replay) >= 2
    assert any(f.get("event") == "session.status" for f in replay)
    assert any(f.get("event") == "session.completed" for f in replay)
