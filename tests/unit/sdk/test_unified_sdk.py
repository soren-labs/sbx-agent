"""Unified SDK contract tests (RFC 167 §08) — transport mocked."""

from __future__ import annotations

import httpx
import pytest

from sbx.sdk.unified import UnifiedApiError, UnifiedClient


def _client(handler):
    transport = httpx.MockTransport(handler)
    return UnifiedClient("http://test", token="tok", http=httpx.Client(transport=transport))


def test_session_create_sends_idem_and_parses():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["idem"] = req.headers.get("idempotency-key")
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(201, json={"session": {"id": "sess_1"}, "turn_id": "turn_1"})

    c = _client(handler)
    out = c.sessions.create("wsp_1", harness={"provider_id": "opencode", "model": "m"})
    assert out["session"]["id"] == "sess_1"
    assert seen["idem"] and seen["auth"] == "Bearer tok"


def test_execute_waits_for_terminal_turn():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/sessions") and req.method == "POST":
            return httpx.Response(201, json={"session": {"id": "sess_9"}})
        if req.url.path.endswith("/messages"):
            return httpx.Response(202, json={"turn_id": "turn_9"})
        if "/turns/" in req.url.path:
            calls["n"] += 1
            state = "running" if calls["n"] < 2 else "succeeded"
            return httpx.Response(200, json={"state": state})
        if "/models" in req.url.path:
            return httpx.Response(200, json={"items": [], "default_model": {"model": "m1"}})
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    c = _client(handler)
    out = c.execute("do it", workspace_id="wsp_1", repository="org/repo", poll_s=0.01)
    assert out["session_id"] == "sess_9"
    assert out["turn_state"] == "succeeded"


def test_error_shape_decodes():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "idempotency_conflict",
                    "category": "concurrency",
                    "message": "key reused",
                    "retryable": False,
                    "details": {},
                    "request_id": "req_1",
                }
            },
        )

    c = _client(handler)
    with pytest.raises(UnifiedApiError) as e:
        c.projects.create("wsp_1", slug="x", name="x")
    assert e.value.status == 409
    assert e.value.code == "idempotency_conflict"
    assert e.value.category == "concurrency"


def test_events_replay_pagination():
    def handler(req: httpx.Request) -> httpx.Response:
        after = int(req.url.params.get("after_seq", "0"))
        items = [{"seq": 1, "type": "t.a"}, {"seq": 2, "type": "t.b"}] if after == 0 else []
        return httpx.Response(200, json={"items": items, "event_watermark": 2})

    c = _client(handler)
    got = list(c.sessions.stream("sess_1", poll_s=0.01, timeout_s=0.05))
    assert [e["seq"] for e in got] == [1, 2]
