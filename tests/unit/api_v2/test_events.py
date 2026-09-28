"""``GET /v2/sessions/{id}/events``: normalized Session SSE + reconnect.

Same mechanics as the V1 run stream — a live ``tail -F`` on
``events.jsonl``, durable-transcript replay once the sandbox is gone —
but frames emit Session-vocabulary types only and carry the session's
turn number.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from tests.unit.api_v1.conftest import V1Env, wait_run
from tests.unit.api_v2.conftest import create_session


def _read_events(
    http: httpx.Client,
    url: str,
    headers: dict[str, str],
    *,
    stop_at: str | None = "turn.finished",
    max_events: int = 80,
    deadline_s: float = 10.0,
) -> tuple[list[tuple[int | None, str, dict]], bool]:
    """Collect (id, event, data) frames until ``stop_at`` or caps."""
    events: list[tuple[int | None, str, dict]] = []
    saw_keepalive = False
    eid: int | None = None
    etype: str | None = None
    deadline = time.monotonic() + deadline_s
    with http.stream("GET", url, headers=headers) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if time.monotonic() > deadline:
                break
            line = line.strip()
            if line.startswith(":"):
                saw_keepalive = True
            elif line.startswith("id:"):
                eid = int(line[3:].strip())
            elif line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                events.append((eid, etype or "", json.loads(line[5:].strip())))
                if etype == stop_at or len(events) >= max_events:
                    break
    return events, saw_keepalive


def _agent_id(env: V1Env, session_id: str) -> str:
    record = env.app.state.task_store.get(session_id)
    assert record is not None and record.agent_id
    return record.agent_id


class TestSessionEvents:
    def test_events_are_normalized(
        self, client: TestClient, auth: dict[str, str], live_base: str, v1_env: V1Env
    ) -> None:
        session = create_session(client, auth)["session"]
        agent_id = _agent_id(v1_env, session["id"])
        wait_run(client, auth, agent_id, "run-1")
        with httpx.Client(base_url=live_base, timeout=10.0) as http:
            events, saw_keepalive = _read_events(http, f"/v2/sessions/{session['id']}/events", auth)
        assert saw_keepalive
        types = [t for _, t, _ in events]
        # The session.status preamble opens every stream (no SSE id).
        assert types[0] == "session.status"
        assert events[0][0] is None
        assert events[0][2]["status"] in ("finished", "running", "queued")
        # Runner/provider internals never reach the wire.
        assert not any(t.startswith("sbx.") for t in types)
        assert "thread.started" not in types
        # Normalized session vocabulary.
        assert "session.meta" in types
        assert "turn.started" in types
        assert "turn.finished" in types
        assert "item.completed" in types
        meta = next(e for _, t, e in events if t == "session.meta")
        assert "account_id" not in meta
        started = next(e for _, t, e in events if t == "turn.started")
        assert started["n"] == 1
        finished = next(e for _, t, e in events if t == "turn.finished")
        assert finished["status"] == "success"
        assert "exit_code" not in finished
        # Line-number ids stay monotonic.
        ids = [eid for eid, _, _ in events if eid is not None]
        assert ids == sorted(ids) and min(ids) >= 1

    def test_last_event_id_resumes(
        self, client: TestClient, auth: dict[str, str], live_base: str, v1_env: V1Env
    ) -> None:
        session = create_session(client, auth)["session"]
        agent_id = _agent_id(v1_env, session["id"])
        wait_run(client, auth, agent_id, "run-1")
        url = f"/v2/sessions/{session['id']}/events"
        with httpx.Client(base_url=live_base, timeout=10.0) as http:
            events, _ = _read_events(http, url, auth)
            numbered = [e for e in events if e[0] is not None]
            assert len(numbered) >= 2
            pivot = numbered[1][0]
            resumed, _ = _read_events(
                http, url, {**auth, "Last-Event-ID": str(pivot)}, deadline_s=4.0
            )
        # The status preamble replays fresh (no id) — it is not resumable state.
        assert resumed[0][1] == "session.status"
        ids = [eid for eid, _, _ in resumed if eid is not None]
        assert ids and min(ids) > pivot

    def test_stream_live_session(
        self, client: TestClient, auth: dict[str, str], live_base: str, monkeypatch
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
        monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "30")
        session = create_session(client, auth)["session"]
        with httpx.Client(base_url=live_base, timeout=10.0) as http:
            events, saw_keepalive = _read_events(
                http,
                f"/v2/sessions/{session['id']}/events",
                auth,
                stop_at="turn.started",
            )
            assert any(t == "turn.started" for _, t, _ in events)
            assert saw_keepalive
        client.post(f"/v2/sessions/{session['id']}/cancel", headers=auth)

    def test_status_frames_emit_transitions(
        self, client: TestClient, auth: dict[str, str], live_base: str, monkeypatch
    ) -> None:
        """session.status frames stream on transitions, not only at connect —
        the console's live phase + terminal-settle logic keys off them."""
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
        monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "4")
        session = create_session(client, auth)["session"]
        url = f"/v2/sessions/{session['id']}/events"
        statuses: list[str] = []
        deadline = time.monotonic() + 15.0
        with httpx.Client(base_url=live_base, timeout=15.0) as http:
            with http.stream("GET", url, headers=auth) as resp:
                assert resp.status_code == 200
                for line in resp.iter_lines():
                    if time.monotonic() > deadline:
                        break
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = json.loads(line[5:].strip())
                    if payload.get("type") != "session.status":
                        continue
                    statuses.append(str(payload.get("status")))
                    if payload.get("status") == "finished":
                        break
        # Connect-time frame says running/queued; the finished frame arrives
        # live — no refetch, no reconnect.
        assert statuses[0] in ("queued", "running")
        assert statuses[-1] == "finished"
        assert len(statuses) >= 2

    def test_closed_session_replays_transcript(
        self, client: TestClient, auth: dict[str, str], live_base: str, v1_env: V1Env
    ) -> None:
        session = create_session(client, auth)["session"]
        agent_id = _agent_id(v1_env, session["id"])
        wait_run(client, auth, agent_id, "run-1")
        # Close the underlying agent (V1 surface) — the sandbox is gone and
        # the stream falls back to the durable per-run transcripts.
        client.delete(f"/v1/agents/{agent_id}", headers=auth)
        with httpx.Client(base_url=live_base, timeout=10.0) as http:
            events, _ = _read_events(
                http,
                f"/v2/sessions/{session['id']}/events",
                auth,
                deadline_s=5.0,
            )
        types = [t for _, t, _ in events]
        assert types[0] == "session.status"
        assert "turn.finished" in types
        assert not any(t.startswith("sbx.") for t in types)
        ids = [eid for eid, _, _ in events if eid is not None]
        assert ids == sorted(ids)

    def test_unknown_event_passes_through(
        self, client: TestClient, auth: dict[str, str], live_base: str, v1_env: V1Env
    ) -> None:
        """Forward-compat: unmapped event types still stream under their
        own name so newer providers degrade gracefully."""
        session = create_session(client, auth)["session"]
        agent_id = _agent_id(v1_env, session["id"])
        wait_run(client, auth, agent_id, "run-1")
        rec = v1_env.store.get(agent_id)
        events_file = Path(rec.handle().root) / "events.jsonl"
        physical = len(events_file.read_text(encoding="utf-8").splitlines())
        with events_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "sbx.test_marker"}) + "\n")
        url = f"/v2/sessions/{session['id']}/events"
        with httpx.Client(base_url=live_base, timeout=10.0) as http:
            events, _ = _read_events(http, url, auth, stop_at="sbx.test_marker")
            marker = [e for e in events if e[1] == "sbx.test_marker"]
            assert marker and marker[0][0] == physical + 1

    def test_events_missing_session_404(self, client: TestClient, auth: dict[str, str]) -> None:
        resp = client.get("/v2/sessions/sess_nope/events", headers=auth)
        assert resp.status_code == 404
