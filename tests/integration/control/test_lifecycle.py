"""Full session lifecycle against LocalProcessBackend + stub_runner."""

from __future__ import annotations

from fastapi.testclient import TestClient
from tests.integration.control.conftest import AUTH
from tests.integration.control.test_api import wait_session


def test_three_turns_stop_close(client: TestClient) -> None:
    created = client.post("/api/sessions", json={"title": "life"}, auth=AUTH)
    assert created.status_code == 201
    sid = created.json()["session_id"]
    wait_session(client, sid, status="idle")

    turn_ids: list[str] = []
    for index, text in enumerate(("alpha", "beta", "gamma"), start=1):
        posted = client.post(f"/api/sessions/{sid}/messages", json={"text": text}, auth=AUTH)
        assert posted.status_code == 202, posted.text
        turn_id = posted.json()["turn_id"]
        turn_ids.append(turn_id)
        session = wait_session(client, sid, status="idle", min_turns=index)
        roles = [m["role"] for m in session["messages"] if m["turn_id"] == turn_id]
        assert "user" in roles
        assert "assistant" in roles
        user = next(
            m for m in session["messages"] if m["turn_id"] == turn_id and m["role"] == "user"
        )
        assert user["text"] == text

    session = client.get(f"/api/sessions/{sid}", auth=AUTH).json()
    assert session["turns"] == 3
    assert session["usage"]["input_tokens"] > 0
    assert session["usage"]["cached_input_tokens"] >= 0
    assert session["cost_estimate_usd"] >= 0
    assert {m["turn_id"] for m in session["messages"] if m["role"] == "user"} == set(turn_ids)

    stop = client.post(f"/api/sessions/{sid}/stop", auth=AUTH)
    assert stop.status_code == 202
    assert stop.json()["status"] == "idle"

    closed = client.delete(f"/api/sessions/{sid}", auth=AUTH)
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    # History survives sandbox recycle.
    replay = client.get(f"/api/sessions/{sid}", auth=AUTH).json()
    assert replay["status"] == "closed"
    assert replay["turns"] == 3
    assert len([m for m in replay["messages"] if m["role"] == "user"]) == 3
    assert len([m for m in replay["messages"] if m["role"] == "assistant"]) == 3
