"""``GET .../runs/{runId}/stream``: SSE frames, run scoping, Last-Event-ID.

Runs against a real uvicorn server (``live_base``): the starlette TestClient
buffers entire responses, so infinite SSE streams can only be read over a
socket.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
from tests.unit.api_v1.conftest import create_agent, wait_run


def _read_events(
    http: httpx.Client,
    url: str,
    headers: dict[str, str],
    *,
    stop_at: str = "sbx.turn_finished",
    max_events: int = 60,
    deadline_s: float = 10.0,
) -> tuple[list[tuple[int, str, dict]], bool]:
    """Collect (id, event, data) frames until ``stop_at`` type or caps."""
    events: list[tuple[int, str, dict]] = []
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
                events.append((eid or 0, etype or "", json.loads(line[5:].strip())))
                if etype == stop_at or len(events) >= max_events:
                    break
    return events, saw_keepalive


class TestRunStream:
    def test_stream_run_frames(self, client, auth, live_base, v1_env) -> None:
        agent = create_agent(client, auth)["agent"]
        with httpx.Client(base_url=live_base, timeout=10.0) as http:
            events, saw_keepalive = _read_events(
                http, f"/v1/agents/{agent['id']}/runs/run-1/stream", auth
            )
        assert saw_keepalive
        types = [etype for _, etype, _ in events]
        assert "sbx.session_meta" in types  # preamble belongs to run 1
        assert "sbx.turn_started" in types
        assert "sbx.turn_finished" in types
        ids = [eid for eid, _, _ in events]
        assert ids == sorted(ids) and min(ids) >= 1
        finished = next(e for _, t, e in events if t == "sbx.turn_finished")
        assert finished["status"] == "success"

    def test_stream_is_scoped_to_run(self, client, auth, live_base) -> None:
        agent = create_agent(client, auth)["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        client.post(
            f"/v1/agents/{agent['id']}/runs",
            json={"prompt": {"text": "second"}},
            headers=auth,
        )
        wait_run(client, auth, agent["id"], "run-2")

        with httpx.Client(base_url=live_base, timeout=10.0) as http:
            events2, _ = _read_events(http, f"/v1/agents/{agent['id']}/runs/run-2/stream", auth)
        assert events2, "expected run-2 events"
        # first frame is turn 2's own marker; nothing from turn 1 leaks in
        first = events2[0]
        assert first[1] == "sbx.turn_started"
        assert first[2]["n"] == 2
        assert all(t != "sbx.session_meta" for _, t, _ in events2)

    def test_last_event_id_resumes(self, client, auth, live_base) -> None:
        agent = create_agent(client, auth)["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        with httpx.Client(base_url=live_base, timeout=10.0) as http:
            events, _ = _read_events(http, f"/v1/agents/{agent['id']}/runs/run-1/stream", auth)
            assert len(events) >= 3
            pivot = events[1][0]
            resumed, _ = _read_events(
                http,
                f"/v1/agents/{agent['id']}/runs/run-1/stream",
                {**auth, "Last-Event-ID": str(pivot)},
            )
        assert resumed, "expected events after Last-Event-ID"
        assert min(eid for eid, _, _ in resumed) > pivot

    def test_stream_live_run(self, client, auth, live_base, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
        monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "30")
        agent = create_agent(client, auth)["agent"]
        with httpx.Client(base_url=live_base, timeout=10.0) as http:
            # stream while the turn is still producing events
            events, saw_keepalive = _read_events(
                http,
                f"/v1/agents/{agent['id']}/runs/run-1/stream",
                auth,
                stop_at="sbx.turn_started",
            )
            assert any(t == "sbx.turn_started" for _, t, _ in events)
            assert saw_keepalive
        client.post(f"/v1/agents/{agent['id']}/runs/run-1/cancel", headers=auth)

    def test_stream_missing_agent_or_run_is_404(self, client, auth) -> None:
        resp = client.get("/v1/agents/nope/runs/run-1/stream", headers=auth)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"
        agent = create_agent(client, auth)["agent"]
        resp = client.get(f"/v1/agents/{agent['id']}/runs/run-9/stream", headers=auth)
        assert resp.status_code == 404

    def test_event_ids_are_physical_events_jsonl_lines(
        self, client, auth, live_base, v1_env
    ) -> None:
        """Contract: ``id`` is the events.jsonl 1-based line number — a
        torn/blank line still consumes one, matching ``/api/*`` semantics."""
        agent = create_agent(client, auth)["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        rec = v1_env.store.get(agent["id"])
        events_file = Path(rec.handle().root) / "events.jsonl"
        physical = len(events_file.read_text(encoding="utf-8").splitlines())
        with events_file.open("a", encoding="utf-8") as fh:
            fh.write("\n")  # a blank line occupies line `physical + 1`
            fh.write(json.dumps({"type": "sbx.test_marker"}) + "\n")

        url = f"/v1/agents/{agent['id']}/runs/run-1/stream"
        with httpx.Client(base_url=live_base, timeout=10.0) as http:
            events, _ = _read_events(http, url, auth, stop_at="sbx.test_marker")
            marker = [e for e in events if e[1] == "sbx.test_marker"]
            assert marker, "appended event never streamed"
            assert marker[0][0] == physical + 2

            # Resume at the last pre-blank line: the marker is the only
            # event after it, still carrying its physical line number.
            resumed, _ = _read_events(
                http,
                url,
                {**auth, "Last-Event-ID": str(physical)},
                stop_at="sbx.test_marker",
            )
            assert [e[0] for e in resumed] == [physical + 2]

    def test_stream_closed_agent_replays_history(self, client, auth, live_base) -> None:
        agent = create_agent(client, auth)["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        client.delete(f"/v1/agents/{agent['id']}", headers=auth)
        # sandbox is gone; the stream still opens and keepalives flow
        with httpx.Client(base_url=live_base, timeout=10.0) as http:
            with http.stream(
                "GET", f"/v1/agents/{agent['id']}/runs/run-1/stream", headers=auth
            ) as resp:
                assert resp.status_code == 200
                first = next(resp.iter_lines())
                assert first.startswith(":")

    def test_closed_agent_replays_durable_transcript(self, client, auth, live_base) -> None:
        agent = create_agent(client, auth)["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        client.delete(f"/v1/agents/{agent['id']}", headers=auth)
        with httpx.Client(base_url=live_base, timeout=10.0) as http:
            events, _ = _read_events(
                http, f"/v1/agents/{agent['id']}/runs/run-1/stream", auth, deadline_s=5.0
            )
        types = [e[1] for e in events]
        assert "sbx.turn_finished" in types
        assert "item.started" not in types
        messages = [
            e[2]["item"]
            for e in events
            if e[1] == "item.completed" and e[2]["item"]["type"] == "agent_message"
        ]
        assert messages
        ids = [e[0] for e in events]
        assert ids == sorted(ids)
