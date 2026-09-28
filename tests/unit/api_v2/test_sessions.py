"""SOR-256: V2 Session API — create/list/detail/follow-up/cancel/retry.

Every assertion is on the public vocabulary — status/phase, session ids,
SessionMessageView — never on the internal Task/Agent/Run ids the facade
maps to.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from tests.unit.api_v2.conftest import (
    V1Env,
    create_session,
    session_body,
    wait_session,
)


def test_create_returns_session_envelope(
    client: TestClient, auth: dict[str, str], credentialed: V1Env
) -> None:
    session = create_session(client, auth, title="first session")
    assert session["id"].startswith("task_")
    assert session["title"] == "first session"
    # The ACK happens before the worker binds an agent — resolve is in flight.
    assert session["status"] == "queued"
    assert session["phase"] == "resolving"
    assert session["created_at"]
    # No Task/Agent/Run ids leak into the public shape.
    assert set(session) <= {
        "id",
        "title",
        "status",
        "phase",
        "execution",
        "repository",
        "usage",
        "error",
        "created_at",
        "updated_at",
    }


def test_create_binds_and_finishes(
    client: TestClient, auth: dict[str, str], credentialed: V1Env
) -> None:
    session = create_session(client, auth)
    done = wait_session(client, auth, session["id"], "finished", "failed")
    assert done["status"] == "finished"
    assert done["phase"] == "finished"
    # Resolved execution evidence is projected, not internal ids.
    assert done["execution"]["provider"] == "codex"
    assert done["execution"]["account_id"] == "acct-codex-1"
    assert done["execution"]["model"] == "gpt-5.6-luna"


def test_detail_is_bounded_first_view(
    client: TestClient, auth: dict[str, str], credentialed: V1Env
) -> None:
    session = create_session(client, auth)
    wait_session(client, auth, session["id"], "finished")
    resp = client.get(f"/v2/sessions/{session['id']}", headers=auth)
    assert resp.status_code == 200
    detail = resp.json()["session"]
    assert detail["id"] == session["id"]
    assert detail["status"] == "finished"
    assert detail["prompt"] == "Create hello.txt."
    roles = [m["role"] for m in detail["messages"]]
    assert roles[0] == "user"
    assert detail["messages"][0]["text"] == "Create hello.txt."
    assert "assistant" in roles
    assert detail["activities"], "expected transcript activity on the first view"
    kinds = {a["kind"] for a in detail["activities"]}
    assert kinds & {"agent_message", "command_execution", "file_change", "reasoning"}
    assert detail["changes"]["status"] in ("none", "unchanged", "ready")
    assert detail["usage"]["input_tokens"] >= 0
    assert detail["cost_estimate_usd"] >= 0.0


def test_list_sessions_paginates(
    client: TestClient, auth: dict[str, str], credentialed: V1Env
) -> None:
    first = create_session(client, auth, title="one")
    second = create_session(client, auth, title="two")
    resp = client.get("/v2/sessions?limit=1", headers=auth)
    assert resp.status_code == 200
    page = resp.json()
    assert len(page["sessions"]) == 1
    assert page["sessions"][0]["id"] == first["id"]
    cursor = page["next_cursor"]
    assert cursor
    page2 = client.get(f"/v2/sessions?limit=1&cursor={cursor}", headers=auth).json()
    assert [s["id"] for s in page2["sessions"]] == [second["id"]]
    assert page2["next_cursor"] is None


def test_create_idempotent_replay(
    client: TestClient, auth: dict[str, str], credentialed: V1Env
) -> None:
    headers = {**auth, "Idempotency-Key": "v2-create-1"}
    first = client.post("/v2/sessions", json=session_body(), headers=headers)
    assert first.status_code == 201
    replay = client.post("/v2/sessions", json=session_body(), headers=headers)
    assert replay.status_code == 201
    assert replay.json()["session"]["id"] == first.json()["session"]["id"]
    conflict = client.post("/v2/sessions", json=session_body(title="different"), headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_resolution_refusal_fails_visibly(
    client: TestClient, auth: dict[str, str], v1_env: V1Env
) -> None:
    """A create whose resolution can never pass lands as a failed session."""
    session = create_session(client, auth)  # account has no auth material
    done = wait_session(client, auth, session["id"], "failed", "finished", timeout=30)
    assert done["status"] == "failed"
    assert done["phase"] == "failed"
    assert done["error"]["code"] in (
        "provider_exhausted",
        "account_unavailable",
        "internal",
    )


def test_get_missing_session_404(client: TestClient, auth: dict[str, str]) -> None:
    resp = client.get("/v2/sessions/task_nope", headers=auth)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_create_extra_field_rejected(
    client: TestClient, auth: dict[str, str], credentialed: V1Env
) -> None:
    resp = client.post("/v2/sessions", json={**session_body(), "agent_id": "sneaky"}, headers=auth)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_follow_up_message(client: TestClient, auth: dict[str, str], credentialed: V1Env) -> None:
    session = create_session(client, auth)
    wait_session(client, auth, session["id"], "finished")
    resp = client.post(
        f"/v2/sessions/{session['id']}/messages",
        json={"prompt": "now do more work"},
        headers=auth,
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] is True
    wait_session(client, auth, session["id"], "finished")
    detail = client.get(f"/v2/sessions/{session['id']}", headers=auth).json()["session"]
    texts = [m["text"] for m in detail["messages"]]
    assert "now do more work" in texts


def test_follow_up_idempotent_replay(
    client: TestClient, auth: dict[str, str], credentialed: V1Env
) -> None:
    session = create_session(client, auth)
    wait_session(client, auth, session["id"], "finished")
    headers = {**auth, "Idempotency-Key": "v2-msg-1"}
    payload = {"prompt": "once more, with feeling"}
    first = client.post(f"/v2/sessions/{session['id']}/messages", json=payload, headers=headers)
    assert first.status_code == 202, first.text
    wait_session(client, auth, session["id"], "finished")
    replay = client.post(f"/v2/sessions/{session['id']}/messages", json=payload, headers=headers)
    assert replay.status_code == 202
    conflict = client.post(
        f"/v2/sessions/{session['id']}/messages",
        json={"prompt": "different"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_message_to_missing_or_dead_session(
    client: TestClient, auth: dict[str, str], credentialed: V1Env
) -> None:
    resp = client.post("/v2/sessions/task_nope/messages", json={"prompt": "hi"}, headers=auth)
    assert resp.status_code == 404


def test_cancel_running_session(
    client: TestClient,
    auth: dict[str, str],
    credentialed: V1Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "30")
    session = create_session(client, auth)
    wait_session(client, auth, session["id"], "running")
    resp = client.post(f"/v2/sessions/{session['id']}/cancel", headers=auth)
    assert resp.status_code == 200, resp.text
    done = wait_session(client, auth, session["id"], "cancelled")
    assert done["status"] == "cancelled"
    assert done["phase"] == "cancelled"
    # Idempotent replay stays cancelled, no error.
    again = client.post(f"/v2/sessions/{session['id']}/cancel", headers=auth)
    assert again.status_code == 200
    assert again.json()["session"]["status"] == "cancelled"


def test_cancel_refuses_new_messages(
    client: TestClient,
    auth: dict[str, str],
    credentialed: V1Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "30")
    session = create_session(client, auth)
    wait_session(client, auth, session["id"], "running")
    client.post(f"/v2/sessions/{session['id']}/cancel", headers=auth)
    wait_session(client, auth, session["id"], "cancelled")
    resp = client.post(
        f"/v2/sessions/{session['id']}/messages", json={"prompt": "still there?"}, headers=auth
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "session_not_runnable"


def test_retry_failed_run(
    client: TestClient,
    auth: dict[str, str],
    credentialed: V1Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "nonzero")
    session = create_session(client, auth)
    done = wait_session(client, auth, session["id"], "failed", "finished")
    assert done["status"] == "failed"
    resp = client.post(f"/v2/sessions/{session['id']}/retry", json={}, headers=auth)
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] is True
    # Scenario still nonzero → the retry runs and fails again, deterministically.
    again = wait_session(client, auth, session["id"], "failed")
    assert again["status"] == "failed"


def test_retry_active_session_409(
    client: TestClient,
    auth: dict[str, str],
    credentialed: V1Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "30")
    session = create_session(client, auth)
    wait_session(client, auth, session["id"], "running")
    resp = client.post(f"/v2/sessions/{session['id']}/retry", json={}, headers=auth)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "task_active"
    client.post(f"/v2/sessions/{session['id']}/cancel", headers=auth)


def test_v1_task_surface_still_works_and_shares_truth(
    client: TestClient, auth: dict[str, str], credentialed: V1Env
) -> None:
    """Regression: a V1 task is the same session under the V2 facade."""
    resp = client.post(
        "/v1/tasks",
        json={"prompt": {"text": "v1 prompt"}},
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["task"]["id"]
    detail = client.get(f"/v2/sessions/{task_id}", headers=auth)
    assert detail.status_code == 200
    session = detail.json()["session"]
    assert session["id"] == task_id
    assert session["prompt"] == "v1 prompt"
    wait_session(client, auth, task_id, "finished", "failed")


def test_warm_create_ack_under_1s(
    client: TestClient, auth: dict[str, str], credentialed: V1Env
) -> None:
    start = time.monotonic()
    resp = client.post("/v2/sessions", json=session_body(), headers=auth)
    elapsed = time.monotonic() - start
    assert resp.status_code == 201, resp.text
    assert elapsed < 1.0


def test_detail_p95_under_1_5s(
    client: TestClient, auth: dict[str, str], credentialed: V1Env
) -> None:
    session = create_session(client, auth)
    wait_session(client, auth, session["id"], "finished")
    samples = []
    for _ in range(10):
        start = time.monotonic()
        resp = client.get(f"/v2/sessions/{session['id']}", headers=auth)
        assert resp.status_code == 200
        samples.append(time.monotonic() - start)
    samples.sort()
    assert samples[int(len(samples) * 0.95)] < 1.5


def test_openapi_document(client: TestClient, auth: dict[str, str]) -> None:
    resp = client.get("/v2/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    paths = spec["paths"]
    for path, methods in {
        "/v2/sessions": {"post", "get"},
        "/v2/sessions/{session_id}": {"get"},
        "/v2/sessions/{session_id}/messages": {"post"},
        "/v2/sessions/{session_id}/events": {"get"},
        "/v2/sessions/{session_id}/cancel": {"post"},
        "/v2/sessions/{session_id}/retry": {"post"},
        "/v2/sessions/{session_id}/changes": {"get"},
        "/v2/sessions/{session_id}/deliver": {"post"},
    }.items():
        assert path in paths, f"missing {path}"
        assert methods <= set(paths[path]), f"{path} missing {methods}"
    canonical = spec["x-canonical"]
    assert canonical["session_statuses"] == [
        "queued",
        "running",
        "finished",
        "failed",
        "cancelled",
    ]
    assert "session_phases" in canonical
    assert "event_types" in canonical
    assert "delivery.updated" in canonical["event_types"]
    assert spec["components"]["schemas"]["ErrorBody"]
    assert spec["components"]["securitySchemes"]["bearerAuth"]
    # No validation-error 422 leaks — the error body is canonical.
    for path_item in paths.values():
        for operation in path_item.values():
            assert "422" not in operation.get("responses", {})


def test_v1_openapi_unaffected(client: TestClient, auth: dict[str, str]) -> None:
    resp = client.get("/v1/openapi.json")
    assert resp.status_code == 200
    assert "/v1/agents" in resp.json()["paths"]
