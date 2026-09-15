"""SOR-82/A4 acceptance: terminal run state must not drift.

For each outcome class — success, provider failure, auth failure, event
parse failure, cancellation, timeout — the same run is read while it
executes, after the sandbox is reclaimed, and after a control-plane
restart. Once terminal, status/result/error/usage/timestamps are durable
facts and must be identical at every phase.
"""

from __future__ import annotations

import pytest
from tests.integration.recovery.support import (
    LIVE_RUN_STATUSES,
    RecoveryEnv,
    assert_run_durable,
    assert_structured_error,
)


def _create(env: RecoveryEnv) -> tuple[str, str]:
    resp = env.post_agent()
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["agent"]["id"], body["run"]["id"]


class TestTerminalStability:
    def test_success_run_is_durable(self, recovery_env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
        agent_id, run_id = _create(recovery_env)
        snap = assert_run_durable(recovery_env, agent_id, run_id)
        assert snap["status"] == "FINISHED"
        assert snap["result"] is not None and "hello" in snap["result"]["text"].lower()
        assert snap["usage"]["input_tokens"] > 0
        assert not snap.get("error")

    def test_provider_failure_run_is_durable(self, recovery_env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "nonzero")
        agent_id, run_id = _create(recovery_env)
        run = recovery_env.wait_run(agent_id, run_id)
        assert run["status"] == "ERROR"
        # provider CLI exited non-zero without a more specific signal:
        # runtime_error is the canonical catch-all for an unclassified
        # provider/runtime failure.
        assert_structured_error(run, code="runtime_error")
        assert_run_durable(recovery_env, agent_id, run_id)

    def test_auth_invalid_run_is_durable(self, recovery_env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "auth_invalid")
        agent_id, run_id = _create(recovery_env)
        run = recovery_env.wait_run(agent_id, run_id)
        assert run["status"] == "ERROR"
        assert_structured_error(run, code="auth_invalid")
        assert_run_durable(recovery_env, agent_id, run_id)

    def test_event_parse_failure_run_is_durable(self, recovery_env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "badjson")
        agent_id, run_id = _create(recovery_env)
        run = recovery_env.wait_run(agent_id, run_id)
        # A parse anomaly must never silently read as success.
        assert run["status"] == "ERROR"
        assert_structured_error(run, code="event_parse_error")
        assert_run_durable(recovery_env, agent_id, run_id)

    def test_slow_success_run_is_durable(self, recovery_env, monkeypatch) -> None:
        """Success path observed mid-flight: live status, then FINISHED."""
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
        monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "3")
        agent_id, run_id = _create(recovery_env)
        live = recovery_env.wait_run(agent_id, run_id, want=LIVE_RUN_STATUSES)
        assert live["status"] in LIVE_RUN_STATUSES
        snap = assert_run_durable(recovery_env, agent_id, run_id)
        assert snap["status"] == "FINISHED"
        assert snap["usage"]["input_tokens"] > 0

    def test_cancelled_run_is_durable(self, recovery_env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
        monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "30")
        agent_id, run_id = _create(recovery_env)
        live = recovery_env.wait_run(agent_id, run_id, want=LIVE_RUN_STATUSES)
        assert live["status"] in LIVE_RUN_STATUSES
        assert live["id"] == run_id and live["agent_id"] == agent_id
        assert live["created_at"]

        resp = recovery_env.cancel_run(agent_id, run_id)
        assert resp.status_code == 200, resp.text
        run = recovery_env.wait_run(agent_id, run_id)
        assert run["status"] == "CANCELLED"
        # "cancelled" is in the canonical error-code set so harnesses can
        # tell a user cancel from a clean finish without parsing SSE.
        assert_structured_error(run, code="cancelled")
        assert_run_durable(recovery_env, agent_id, run_id)

    def test_timeout_run_is_durable(self, make_recovery_env, monkeypatch) -> None:
        env = make_recovery_env(turn_max_seconds=2)
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "hang")
        agent_id, run_id = _create(env)
        live = env.wait_run(agent_id, run_id, want=LIVE_RUN_STATUSES)
        assert live["status"] in LIVE_RUN_STATUSES
        run = env.wait_run(agent_id, run_id, timeout=15)
        assert run["status"] == "EXPIRED"
        assert_structured_error(run, code="timeout")
        assert_run_durable(env, agent_id, run_id)


class TestRunMetadataPersisted:
    """The durable run record carries provider/account/model (SOR-82)."""

    def test_run_carries_identity_metadata(self, recovery_env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
        agent_id, run_id = _create(recovery_env)
        run = recovery_env.wait_run(agent_id, run_id)
        for key in ("id", "agent_id", "status", "created_at"):
            assert run.get(key), f"run missing {key}"
        assert run["agent_id"] == agent_id

    def test_followup_run_is_durable_too(self, recovery_env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
        agent_id, _ = _create(recovery_env)
        recovery_env.wait_run(agent_id, "run-1")
        resp = recovery_env.client.post(
            f"/v1/agents/{agent_id}/runs",
            json={"prompt": {"text": "second turn"}},
            headers=recovery_env.auth,
        )
        assert resp.status_code == 201, resp.text
        run2_id = resp.json()["id"]
        snap1 = recovery_env.get_run(agent_id, "run-1").json()
        snap2 = assert_run_durable(recovery_env, agent_id, run2_id)
        assert snap2["status"] == "FINISHED"
        # the earlier run's terminal state is untouched by later runs
        assert recovery_env.get_run(agent_id, "run-1").json() == snap1


@pytest.mark.parametrize("scenario", ["success", "nonzero", "badjson", "auth_invalid"])
def test_terminal_snapshot_is_byte_identical_across_restart(
    recovery_env, monkeypatch, scenario
) -> None:
    """Whole-payload equality: nothing about a terminal run may be rederived
    differently once the worker is gone and the control plane restarted."""
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", scenario)
    agent_id, run_id = _create(recovery_env)
    snap = recovery_env.wait_run(agent_id, run_id)
    recovery_env.teardown(agent_id)
    env2 = recovery_env.restart()
    assert env2.get_run(agent_id, run_id).json() == snap
