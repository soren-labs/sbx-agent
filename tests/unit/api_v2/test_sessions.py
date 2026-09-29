"""``/v2/sessions``: create/list/detail/follow-up/cancel/retry + schema checks.

The engine is V1's Task/Agent/Run machinery; every assertion here guards
the Session-first contract — stable status vocabulary, sanitized payload,
bounded first view.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.unit.api_v1.conftest import V1Env, wait_run
from tests.unit.api_v2.conftest import create_session, wait_session

# Internal namespaces and engine internals that must never cross the /v2
# projection boundary.
FORBIDDEN_KEYS = {
    "agent_id",
    "task_id",
    "run_id",
    "artifact_id",
    "artifact_refs",
    "account_id",
    "candidates",
    "evidence",
    "resolved",
    "request",
    "transitions",
    "idempotency",
    "sandbox_tags",
    "handle",
    "workdir",
}


def _assert_no_leaks(obj: Any, path: str = "") -> None:
    """Recursively forbid engine internals anywhere in a /v2 payload."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert k not in FORBIDDEN_KEYS, f"{path or '<root>'}.{k} leaks"
            _assert_no_leaks(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _assert_no_leaks(v, f"{path}[{i}]")


def _assert_session_shape(session: dict[str, Any]) -> None:
    assert session["id"].startswith("sess_")
    assert isinstance(session["prompt"], str) and session["prompt"]
    assert session["status"] in ("queued", "running", "finished", "failed", "cancelled")
    assert session["phase"] in (
        "provisioning",
        "queued",
        "running",
        "delivering",
        "finished",
        "failed",
        "cancelled",
    )
    _assert_no_leaks(session)


class TestCreateSession:
    def test_create_minimal(self, client: TestClient, auth: dict[str, str]) -> None:
        resp = client.post("/v2/sessions", json={"prompt": "do the thing"}, headers=auth)
        assert resp.status_code == 201, resp.text
        session = resp.json()["session"]
        _assert_session_shape(session)
        assert session["prompt"] == "do the thing"
        # create acknowledges while the run is still in flight.
        assert session["status"] in ("queued", "running")

    def test_create_full_fields(self, client: TestClient, auth: dict[str, str], git_repo) -> None:
        repo_url, sha = git_repo
        body = {
            "prompt": "full body",
            "title": "titled session",
            "repository": {"repo": repo_url},
            "execution": {"provider": "codex", "model": "gpt-5.6-luna"},
            "delivery": {"branch": "feat/x", "auto_publish": False},
            "advanced": {"idle_timeout_s": 60},
        }
        resp = client.post("/v2/sessions", json=body, headers=auth)
        assert resp.status_code == 201, resp.text
        session = resp.json()["session"]
        _assert_session_shape(session)
        assert session["title"] == "titled session"
        assert session["repository"]["repo"] == repo_url
        assert session["repository"]["base_sha"] == sha
        # Resolved execution carries the effective pick — never the
        # candidate list or evidence.
        assert session["execution"]["provider"] == "codex"
        assert session["execution"]["model"] == "gpt-5.6-luna"

    def test_create_requires_prompt(self, client: TestClient, auth: dict[str, str]) -> None:
        resp = client.post("/v2/sessions", json={"title": "x"}, headers=auth)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_request"

    def test_create_rejects_unknown_field(self, client: TestClient, auth: dict[str, str]) -> None:
        resp = client.post(
            "/v2/sessions",
            json={"prompt": "x", "agent_id": "whatever"},
            headers=auth,
        )
        assert resp.status_code == 400

    def test_create_idempotent_replay(self, client: TestClient, auth: dict[str, str]) -> None:
        headers = {**auth, "Idempotency-Key": "v2-create-1"}
        body = {"prompt": "idempotent me"}
        first = client.post("/v2/sessions", json=body, headers=headers)
        assert first.status_code == 201
        replay = client.post("/v2/sessions", json=body, headers=headers)
        assert replay.status_code == 201
        assert replay.json()["session"]["id"] == first.json()["session"]["id"]
        conflict = client.post("/v2/sessions", json={"prompt": "different"}, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"


class TestGetAndList:
    def test_detail_bounded_first_view(self, client: TestClient, auth: dict[str, str]) -> None:
        session = create_session(client, auth)["session"]
        detail = client.get(f"/v2/sessions/{session['id']}", headers=auth)
        assert detail.status_code == 200
        body = detail.json()
        _assert_no_leaks(body)
        _assert_session_shape(body["session"])
        assert body["run_count"] >= 1
        assert body["truncated"] is False
        run = body["runs"][0]
        assert run["n"] == 1
        assert run["prompt"] == session["prompt"]
        assert run["status"] in ("queued", "running", "finished", "failed", "cancelled")

    def test_detail_after_finish(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env
    ) -> None:
        session = create_session(client, auth)["session"]
        task_store = v1_env.app.state.task_store
        wait_run(client, auth, task_store.get(session["id"]).agent_id, "run-1")
        detail = client.get(f"/v2/sessions/{session['id']}", headers=auth).json()
        assert detail["session"]["status"] == "finished"
        assert detail["session"]["phase"] == "finished"
        run = detail["runs"][0]
        assert run["status"] == "finished"
        assert run["result"]
        assert detail["session"]["turns"] >= 1
        assert detail["session"]["usage"]["input_tokens"] >= 0

    def test_list_newest_first(self, client: TestClient, auth: dict[str, str]) -> None:
        a = create_session(client, auth, prompt="first")["session"]
        b = create_session(client, auth, prompt="second")["session"]
        listing = client.get("/v2/sessions", headers=auth).json()
        ids = [s["id"] for s in listing["sessions"]]
        assert ids[:2] == [b["id"], a["id"]]
        assert listing["total"] == 2
        for s in listing["sessions"]:
            _assert_session_shape(s)

    def test_list_pagination(self, client: TestClient, auth: dict[str, str]) -> None:
        for i in range(3):
            create_session(client, auth, prompt=f"p{i}")
        page = client.get("/v2/sessions?limit=2&offset=0", headers=auth).json()
        assert len(page["sessions"]) == 2
        assert page["total"] == 3
        page2 = client.get("/v2/sessions?limit=2&offset=2", headers=auth).json()
        assert len(page2["sessions"]) == 1

    def test_list_excludes_v1_tasks(self, client: TestClient, auth: dict[str, str]) -> None:
        # A task created on the V1 surface is not a session.
        resp = client.post(
            "/v1/tasks",
            json={"prompt": {"text": "v1 task"}, "execution": {"provider": "codex"}},
            headers=auth,
        )
        assert resp.status_code == 201, resp.text
        listing = client.get("/v2/sessions", headers=auth).json()
        assert listing["total"] == 0
        assert listing["sessions"] == []

    def test_session_not_visible_to_other_keys(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env
    ) -> None:
        session = create_session(client, auth)["session"]
        _, other_token = v1_env.keys.create(label="other", scopes=("agents",))
        other = {"Authorization": f"Bearer {other_token}"}
        for method, url in (
            ("GET", f"/v2/sessions/{session['id']}"),
            ("GET", f"/v2/sessions/{session['id']}/changes"),
        ):
            resp = client.request(method, url, headers=other)
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"]["code"] == "not_found"
        for url, body in (
            (f"/v2/sessions/{session['id']}/messages", {"prompt": "x"}),
            (f"/v2/sessions/{session['id']}/cancel", {}),
            (f"/v2/sessions/{session['id']}/retry", {}),
            (f"/v2/sessions/{session['id']}/deliver", {}),
        ):
            resp = client.post(url, json=body, headers=other)
            assert resp.status_code == 404, resp.text

    def test_task_id_is_not_a_session(self, client: TestClient, auth: dict[str, str]) -> None:
        resp = client.post(
            "/v1/tasks",
            json={"prompt": {"text": "v1 task"}, "execution": {"provider": "codex"}},
            headers=auth,
        )
        task = resp.json()["task"]
        assert client.get(f"/v2/sessions/{task['id']}", headers=auth).status_code == 404

    def test_session_appears_on_v1_engine(self, client: TestClient, auth: dict[str, str]) -> None:
        """Engine sharing is real: a session is a task record the V1
        surface can still inspect (V1 behavior preserved)."""
        session = create_session(client, auth)["session"]
        tasks = client.get("/v1/tasks", headers=auth).json()["tasks"]
        assert any(t["id"] == session["id"] for t in tasks)


class TestMessages:
    def test_follow_up(self, client: TestClient, auth: dict[str, str]) -> None:
        session = create_session(client, auth)["session"]
        wait_session(client, auth, session["id"], "finished")
        resp = client.post(
            f"/v2/sessions/{session['id']}/messages",
            json={"prompt": "also do this"},
            headers=auth,
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        _assert_no_leaks(body)
        assert body["message"]["n"] == 2
        assert body["session"]["status"] in ("queued", "running", "finished")
        wait_session(client, auth, session["id"], "finished")
        detail = client.get(f"/v2/sessions/{session['id']}", headers=auth).json()
        assert detail["run_count"] == 2
        assert detail["runs"][1]["prompt"] == "also do this"

    def test_message_on_missing_session(self, client: TestClient, auth: dict[str, str]) -> None:
        resp = client.post("/v2/sessions/sess_nope/messages", json={"prompt": "x"}, headers=auth)
        assert resp.status_code == 404

    def test_message_empty_prompt(self, client: TestClient, auth: dict[str, str]) -> None:
        session = create_session(client, auth)["session"]
        resp = client.post(
            f"/v2/sessions/{session['id']}/messages", json={"prompt": ""}, headers=auth
        )
        assert resp.status_code == 400


class TestCancel:
    def test_cancel_running_session(
        self, client: TestClient, auth: dict[str, str], v1_env: V1Env, monkeypatch
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
        monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "30")
        session = create_session(client, auth)["session"]
        resp = client.post(f"/v2/sessions/{session['id']}/cancel", headers=auth)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        _assert_no_leaks(body)
        # Cancel is sticky: the session settles to cancelled, never running.
        final = wait_session(client, auth, session["id"], "cancelled", timeout=15)
        assert final["session"]["status"] == "cancelled"
        # Idempotent replay.
        again = client.post(f"/v2/sessions/{session['id']}/cancel", headers=auth)
        assert again.status_code == 200
        assert again.json()["session"]["status"] == "cancelled"

    def test_cancel_finished_is_noop(self, client: TestClient, auth: dict[str, str]) -> None:
        session = create_session(client, auth)["session"]
        wait_session(client, auth, session["id"], "finished")
        resp = client.post(f"/v2/sessions/{session['id']}/cancel", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["session"]["status"] == "finished"


class TestRetry:
    def test_retry_failed_session(
        self, client: TestClient, auth: dict[str, str], monkeypatch
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "nonzero")
        session = create_session(client, auth)["session"]
        final = wait_session(client, auth, session["id"], "failed")
        assert final["session"]["error"] is not None
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
        resp = client.post(f"/v2/sessions/{session['id']}/retry", json={}, headers=auth)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        _assert_no_leaks(body)
        wait_session(client, auth, session["id"], "finished")
        detail = client.get(f"/v2/sessions/{session['id']}", headers=auth).json()
        assert detail["run_count"] == 2
        assert detail["runs"][1]["status"] == "finished"

    def test_retry_clears_stale_error(
        self, client: TestClient, auth: dict[str, str], monkeypatch
    ) -> None:
        """B6: a failed attempt's error stays on its own run row — after a
        successful retry the session-level error is empty on every read."""
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "auth_invalid")
        session = create_session(client, auth)["session"]
        failed = wait_session(client, auth, session["id"], "failed")
        assert failed["session"]["status"] == "failed"
        assert failed["session"]["error"]["code"] == "auth_invalid"
        assert failed["runs"][0]["error"]["code"] == "auth_invalid"

        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
        resp = client.post(f"/v2/sessions/{session['id']}/retry", json={}, headers=auth)
        assert resp.status_code == 200, resp.text
        _assert_no_leaks(resp.json())
        # The retried attempt is the current one — the stale error is gone
        # even before the new run finishes.
        assert resp.json()["session"]["error"] is None

        done = wait_session(client, auth, session["id"], "finished")
        assert done["session"]["error"] is None

        # Post-refresh detail read agrees, and the failed attempt stays
        # traceable on its own run row.
        detail = client.get(f"/v2/sessions/{session['id']}", headers=auth).json()
        assert detail["session"]["status"] == "finished"
        assert detail["session"]["error"] is None
        assert detail["run_count"] == 2
        assert detail["runs"][0]["status"] == "failed"
        assert detail["runs"][0]["error"]["code"] == "auth_invalid"
        assert detail["runs"][1]["error"] is None

        # A list read cannot resurrect it either.
        listed = client.get("/v2/sessions", headers=auth).json()
        row = next(s for s in listed["sessions"] if s["id"] == session["id"])
        assert row["status"] == "finished"
        assert row["error"] is None

    def test_retry_active_session_conflicts(
        self, client: TestClient, auth: dict[str, str], monkeypatch
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
        monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "30")
        session = create_session(client, auth)["session"]
        wait_session(client, auth, session["id"], "running")
        resp = client.post(f"/v2/sessions/{session['id']}/retry", json={}, headers=auth)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "task_active"
        client.post(f"/v2/sessions/{session['id']}/cancel", headers=auth)


class TestStatusMapping:
    """Pure mapping coverage — the aggregate → session vocabulary table."""

    @pytest.mark.parametrize(
        "aggregate,reason,want_status,want_phase",
        [
            ("queued", "awaiting_dispatch", "queued", "provisioning"),
            ("queued", "queued_work", "queued", "queued"),
            ("running", "run_active", "running", "running"),
            ("delivering", "delivery_pending", "running", "delivering"),
            ("finished", "run_finished", "finished", "finished"),
            ("error", "run_error", "failed", "failed"),
            ("error", "session_lost", "failed", "failed"),
            ("expired", "run_expired", "failed", "failed"),
            ("delivery_failed", "delivery_failed", "failed", "failed"),
            ("cancelled", "task_cancelled", "cancelled", "cancelled"),
            ("cancelled", "session_closed", "cancelled", "cancelled"),
        ],
    )
    def test_map(self, aggregate, reason, want_status, want_phase) -> None:
        from control.api_v2.projection import map_session_status

        assert map_session_status(aggregate, reason) == (want_status, want_phase)

    @pytest.mark.parametrize(
        "run_status,want",
        [
            ("CREATING", "queued"),
            ("QUEUED", "queued"),
            ("RUNNING", "running"),
            ("FINISHED", "finished"),
            ("ERROR", "failed"),
            ("EXPIRED", "failed"),
            ("CANCELLED", "cancelled"),
            ("UNKNOWN", "failed"),
        ],
    )
    def test_run_map(self, run_status, want) -> None:
        from control.api_v2.projection import map_run_status

        assert map_run_status(run_status) == want

    def test_event_normalize(self) -> None:
        from control.api_v2.events import normalize, track_turn

        # sbx.turn_started → turn.started carrying n; it also marks the
        # boundary so following events annotate the same n.
        turn = track_turn({"type": "sbx.turn_started", "n": 2}, 0)
        assert turn == 2
        assert normalize({"type": "sbx.turn_started", "n": 2}, turn) == {
            "type": "turn.started",
            "n": 2,
        }
        # account_id and exit_code never cross the boundary.
        meta = normalize(
            {"type": "sbx.session_meta", "provider": "codex", "model": "m", "account_id": "acct-1"},
            0,
        )
        assert meta == {"type": "session.meta", "provider": "codex", "model": "m"}
        fin = normalize(
            {"type": "sbx.turn_finished", "status": "success", "exit_code": 0, "usage": {}},
            turn,
        )
        assert fin["type"] == "turn.finished" and "exit_code" not in fin
        # Provider-internal markers are dropped; canonical items pass.
        assert normalize({"type": "thread.started"}, 0) is None
        assert normalize({"type": "turn.started"}, 0) is None
        item = {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message"}}
        assert normalize(item, turn) == {**item, "n": 2}
        # Unknown frames pass through for forward compatibility.
        assert normalize({"type": "sbx.future"}, 0) == {"type": "sbx.future"}


class TestV1Regression:
    """The facade shares the engine — V1 calls keep working beside it."""

    def test_v1_agent_lifecycle_unchanged(self, client: TestClient, auth: dict[str, str]) -> None:
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "v1 still works"},
                "agent": {"provider": "codex"},
            },
            headers=auth,
        )
        assert resp.status_code == 201, resp.text
        agent = resp.json()["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "FINISHED"
