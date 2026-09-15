"""Structured run errors on GET /v1 runs (SOR-82/A3).

GET must expose the failure cause as ``run.error`` — callers never parse
SSE to learn why a run failed. Event-parse corruption is an error even
when a ``turn.completed`` still arrived.
"""

from __future__ import annotations

from tests.unit.api_v1.conftest import create_agent, wait_run


class TestStructuredRunErrors:
    def test_provider_failure_exposes_structured_error(self, client, auth, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "nonzero")
        agent = create_agent(client, auth)["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "ERROR"
        error = run["error"]
        assert error["code"] == "runtime_error"
        assert error["source"] == "provider"
        assert "fake_codex nonzero scenario" in error["message"]
        assert error["retryable"] is False

    def test_auth_failure_maps_auth_invalid(self, client, auth, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "auth_invalid")
        agent = create_agent(client, auth)["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "ERROR"
        error = run["error"]
        assert error["code"] == "auth_invalid"
        assert error["source"] == "provider"
        assert "401" in error["message"]

    def test_bad_json_never_becomes_success(self, client, auth, monkeypatch) -> None:
        # The fixture has a malformed line followed by a valid
        # turn.completed — execution completeness is not independently
        # confirmed, so the run stays ERROR with event_parse_error.
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "badjson")
        agent = create_agent(client, auth)["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "ERROR"
        error = run["error"]
        assert error["code"] == "event_parse_error"
        assert error["source"] == "telemetry"

    def test_timeout_maps_expired_timeout_error(self, client, auth, v1_env, monkeypatch) -> None:
        v1_env.app.state.plane.turn_max_seconds = 2
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "hang")
        agent = create_agent(client, auth)["agent"]
        run = wait_run(client, auth, agent["id"], "run-1", timeout=30.0)
        assert run["status"] == "EXPIRED"
        error = run["error"]
        assert error["code"] == "timeout"
        assert error["source"] == "runtime"
        assert error["retryable"] is True

    def test_cancelled_run_exposes_cancelled_error(self, client, auth, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
        monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "30")
        agent = create_agent(client, auth)["agent"]
        resp = client.post(f"/v1/agents/{agent['id']}/runs/run-1/cancel", headers=auth)
        assert resp.status_code == 200
        run = resp.json()
        assert run["status"] == "CANCELLED"
        error = run["error"]
        assert error["code"] == "cancelled"
        assert error["source"] == "control"

    def test_successful_run_has_null_error(self, client, auth) -> None:
        agent = create_agent(client, auth)["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "FINISHED"
        assert run["error"] is None
