"""SOR-139 acceptance: a control-plane cutover must not strand a run.

A session whose turn watcher lived on the drained container stays
``running`` with the ledger run open — even when the provider already
wrote ``turns/<n>.json``. On-demand reconciliation (any ``/v1`` read or
write of the agent) settles the durable truth from evidence: run
FINISHED, agent idle, follow-ups and publish-ready gating unblocked.
Absent or unreadable evidence leaves the record untouched — the reaper's
``run_grace_s`` bound owns the genuinely-wedged case.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from control.backend import SandboxSpec
from control.store import SessionRecord, empty_usage
from tests.integration.recovery.support import RecoveryEnv


def _stranded_session(
    env: RecoveryEnv,
    agent_id: str,
    *,
    payload: dict | None,
    kill_sandbox: bool = False,
) -> None:
    """Fabricate the post-cutover state: ``running`` record, no watcher.

    The record is written straight into the durable store and the sandbox
    is allocated on the live backend — exactly what a drained container
    leaves behind when its ``_watch_turn`` threads die mid-turn.
    """
    handle = env.backend.create(
        SandboxSpec(
            tags={
                "session_id": agent_id,
                "owner": "k",
                "provider": "codex",
                "account_id": "acct-codex-1",
            }
        )
    )
    if payload is not None:
        (handle.root / "turns").mkdir(parents=True, exist_ok=True)
        (handle.root / "turns" / "1.json").write_text(json.dumps(payload))
    if kill_sandbox:
        env.backend.terminate(handle)
    now = datetime.now(UTC)
    env.store.put(
        SessionRecord(
            id=agent_id,
            title="stranded",
            status="running",
            created_at=now,
            updated_at=now,
            model="gpt-5.6-luna",
            turns=0,
            usage=empty_usage(),
            messages=[
                {"role": "user", "text": "hello", "turn_id": "turn-1", "ts": now.isoformat()}
            ],
            owner="k",
            sandbox_id=handle.id,
            sandbox_root=str(handle.root),
            sandbox_tags=dict(handle.tags),
            current_turn_id="turn-1",
            current_turn_n=1,
            last_activity_at=now,
        )
    )
    env.app.state.plane.run_ledger.begin(
        agent_id=agent_id, n=1, provider="codex", account_id="acct-codex-1"
    )


_SUCCESS_PAYLOAD = {
    "n": 1,
    "status": "success",
    "usage": {"input_tokens": 12, "cached_input_tokens": 0, "output_tokens": 5},
    "message": "provider finished while the watcher was gone",
    "exit_code": 0,
}


def test_stranded_success_run_finalizes_on_agent_read(recovery_env: RecoveryEnv) -> None:
    _stranded_session(recovery_env, "agt-stranded", payload=_SUCCESS_PAYLOAD)
    env2 = recovery_env.restart()  # watcher threads died with the old container

    resp = env2.get_agent("agt-stranded")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "idle"

    run = env2.get_run("agt-stranded", "run-1")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "FINISHED"
    assert body["result"]["text"] == "provider finished while the watcher was gone"
    assert not body.get("error")


def test_reconciled_agent_accepts_follow_up_run(recovery_env: RecoveryEnv) -> None:
    _stranded_session(recovery_env, "agt-stranded", payload=_SUCCESS_PAYLOAD)
    env2 = recovery_env.restart()

    resp = env2.client.post(
        "/v1/agents/agt-stranded/runs",
        json={"prompt": {"text": "do another thing"}},
        headers=env2.auth,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] == "run-2"

    finished = env2.wait_run("agt-stranded", "run-2")
    assert finished["status"] == "FINISHED"
    # The durable verdict precedes eager checkpoint settlement. The live
    # watcher correctly keeps the agent busy until that work completes.
    until = time.monotonic() + 5
    while time.monotonic() < until:
        agent = env2.get_agent("agt-stranded").json()
        if agent["status"] == "idle":
            break
        time.sleep(0.01)
    assert agent["status"] == "idle"


def test_cancel_stranded_success_run_reports_finished_not_cancelled(
    recovery_env: RecoveryEnv,
) -> None:
    _stranded_session(recovery_env, "agt-stranded", payload=_SUCCESS_PAYLOAD)
    env2 = recovery_env.restart()

    resp = env2.cancel_run("agt-stranded", "run-1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "FINISHED"


def test_stranded_run_without_evidence_stays_open(recovery_env: RecoveryEnv) -> None:
    # Live sandbox, no turn payload: the provider may still be writing —
    # absent evidence must not finalize anything.
    _stranded_session(recovery_env, "agt-stranded", payload=None)
    env2 = recovery_env.restart()

    resp = env2.get_agent("agt-stranded")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"
    run = env2.get_run("agt-stranded", "run-1")
    assert run.status_code == 200, run.text
    assert run.json()["status"] == "RUNNING"


def test_stranded_run_dead_sandbox_stays_running_for_reaper(
    recovery_env: RecoveryEnv,
) -> None:
    # Evidence unreadable because the sandbox is gone: the reaper owns the
    # session transition — reconciliation must not race it to a verdict.
    _stranded_session(recovery_env, "agt-stranded", payload=_SUCCESS_PAYLOAD, kill_sandbox=True)
    env2 = recovery_env.restart()

    resp = env2.get_agent("agt-stranded")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"
    run = env2.get_run("agt-stranded", "run-1")
    assert run.status_code == 200, run.text
    assert run.json()["status"] == "RUNNING"
