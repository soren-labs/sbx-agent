"""SOR-82/A4 acceptance: missing or corrupt historical evidence must never
fall back to FINISHED.

The pre-SOR-82 read path re-derives terminal state from ``turns/<n>.json``
inside the sandbox; once the sandbox is reclaimed (or the file is missing /
corrupt) an ERROR run silently reads as FINISHED. The durable-run contract
forbids that: a run reads as its persisted terminal state, or as an explicit
unknown/unavailable — never as fake success.
"""

from __future__ import annotations

from tests.integration.recovery.support import (
    LIVE_RUN_STATUSES,
    assert_structured_error,
)


def _turn_file(env, agent_id: str, n: int = 1):
    handle = env.sandbox_handle(agent_id)
    assert handle is not None
    return handle.root / "turns" / f"{n}.json"


def _finish_error_run(env, monkeypatch, scenario: str = "nonzero") -> tuple[str, str]:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", scenario)
    resp = env.post_agent()
    assert resp.status_code == 201, resp.text
    body = resp.json()
    agent_id, run_id = body["agent"]["id"], body["run"]["id"]
    run = env.wait_run(agent_id, run_id)
    assert run["status"] == "ERROR"
    return agent_id, run_id


def test_error_run_stays_error_after_sandbox_teardown(recovery_env, monkeypatch) -> None:
    env = recovery_env
    agent_id, run_id = _finish_error_run(env, monkeypatch)
    env.teardown(agent_id)
    run = env.get_run(agent_id, run_id)
    assert run.status_code == 200
    assert run.json()["status"] == "ERROR"  # today this drifts to FINISHED


def test_error_run_stays_error_in_run_listing_after_teardown(recovery_env, monkeypatch) -> None:
    env = recovery_env
    agent_id, run_id = _finish_error_run(env, monkeypatch)
    env.teardown(agent_id)
    runs = env.list_runs(agent_id).json()["runs"]
    by_id = {r["id"]: r for r in runs}
    assert by_id[run_id]["status"] == "ERROR"


def test_corrupt_turn_payload_never_becomes_finished(recovery_env, monkeypatch) -> None:
    env = recovery_env
    agent_id, run_id = _finish_error_run(env, monkeypatch)
    _turn_file(env, agent_id).write_text("{ this is not json\n", encoding="utf-8")
    run = env.get_run(agent_id, run_id)
    assert run.status_code == 200
    # The persisted truth was ERROR; corrupt evidence must not improve it.
    assert run.json()["status"] == "ERROR"
    assert_structured_error(run.json())


def test_missing_turn_payload_never_becomes_finished(recovery_env, monkeypatch) -> None:
    env = recovery_env
    agent_id, run_id = _finish_error_run(env, monkeypatch)
    _turn_file(env, agent_id).unlink()
    run = env.get_run(agent_id, run_id)
    assert run.status_code == 200
    assert run.json()["status"] == "ERROR"
    assert_structured_error(run.json())


def test_killed_mid_run_never_reports_finished(recovery_env, monkeypatch) -> None:
    """A worker dying mid-run is a failure — not FINISHED, not a user cancel."""
    env = recovery_env
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "30")
    resp = env.post_agent()
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["agent"]["id"]
    run_id = resp.json()["run"]["id"]
    env.wait_run(agent_id, run_id, want=LIVE_RUN_STATUSES)

    env.teardown(agent_id)  # worker lost mid-run
    run = env.wait_run(agent_id, run_id, timeout=15)
    assert run["status"] != "FINISHED"
    assert run["status"] in ("ERROR", "EXPIRED"), (
        f"worker loss must surface as a failure, got {run['status']}"
    )
    if run["status"] == "ERROR":
        assert_structured_error(run)

    env2 = env.restart()
    again = env2.get_run(agent_id, run_id)
    assert again.status_code == 200
    assert again.json()["status"] == run["status"]
    assert again.json()["status"] != "FINISHED"


def test_deleted_agent_record_never_reads_finished(recovery_env, monkeypatch) -> None:
    env = recovery_env
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
    resp = env.post_agent()
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["agent"]["id"]
    run_id = resp.json()["run"]["id"]
    env.wait_run(agent_id, run_id)

    env.store.delete(agent_id)
    got = env.get_run(agent_id, run_id)
    assert not (got.status_code == 200 and got.json().get("status") == "FINISHED")


def test_corrupt_session_record_never_reads_finished(recovery_env, monkeypatch) -> None:
    env = recovery_env
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
    resp = env.post_agent()
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["agent"]["id"]
    run_id = resp.json()["run"]["id"]
    env.wait_run(agent_id, run_id)

    # Simulate a damaged durable record by writing a shape SessionRecord
    # cannot deserialize straight into the store.
    env.store._items[agent_id] = {"id": agent_id, "garbage": True}
    got = env.get_run(agent_id, run_id)
    assert not (got.status_code == 200 and got.json().get("status") == "FINISHED")
