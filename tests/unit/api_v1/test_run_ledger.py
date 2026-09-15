"""SOR-82/A1: durable run ledger through the /v1 API.

Terminal run state is persisted at transition time; sandbox teardown or a
control-plane restart must not change it. Missing/corrupt evidence reports
explicit UNKNOWN — never inferred FINISHED.
"""

from __future__ import annotations

import sys

from control.app import create_app
from control.run_store import FileRunStore
from fastapi.testclient import TestClient
from tests.unit.api_v1.conftest import create_agent, wait_run


def _get_run(client, auth, agent_id, run_id="run-1"):
    resp = client.get(f"/v1/agents/{agent_id}/runs/{run_id}", headers=auth)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestTerminalDurability:
    def test_finished_run_survives_teardown(self, client, auth) -> None:
        agent = create_agent(client, auth)["agent"]
        before = wait_run(client, auth, agent["id"], "run-1")
        assert before["status"] == "FINISHED"

        client.delete(f"/v1/agents/{agent['id']}", headers=auth)

        after = _get_run(client, auth, agent["id"])
        for key in ("status", "result", "usage", "error", "created_at", "finished_at"):
            assert after[key] == before[key], key
        assert after["result"]["text"]
        assert after["usage"]["output_tokens"] >= 0

    def test_error_run_never_drifts_to_finished(self, client, auth, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "nonzero")
        agent = create_agent(client, auth)["agent"]
        before = wait_run(client, auth, agent["id"], "run-1")
        assert before["status"] == "ERROR"
        assert before["error"]["code"] == "runtime_error"

        client.delete(f"/v1/agents/{agent['id']}", headers=auth)

        after = _get_run(client, auth, agent["id"])
        assert after["status"] == "ERROR"
        assert after["error"]["code"] == "runtime_error"
        runs = client.get(f"/v1/agents/{agent['id']}/runs", headers=auth).json()["runs"]
        assert [r["status"] for r in runs] == ["ERROR"]

    def test_cancelled_run_persists_after_teardown(self, client, auth, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
        monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "30")
        agent = create_agent(client, auth)["agent"]
        resp = client.post(f"/v1/agents/{agent['id']}/runs/run-1/cancel", headers=auth)
        assert resp.json()["status"] == "CANCELLED"

        client.delete(f"/v1/agents/{agent['id']}", headers=auth)
        after = _get_run(client, auth, agent["id"])
        assert after["status"] == "CANCELLED"
        assert after["error"]["code"] == "cancelled"

    def test_expired_run_persists_after_teardown(self, client, auth, v1_env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
        monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "30")
        v1_env.app.state.plane.turn_max_seconds = 1
        agent = create_agent(client, auth)["agent"]
        before = wait_run(client, auth, agent["id"], "run-1")
        assert before["status"] == "EXPIRED"
        assert before["error"]["code"] == "timeout"

        client.delete(f"/v1/agents/{agent['id']}", headers=auth)
        assert _get_run(client, auth, agent["id"])["status"] == "EXPIRED"


class TestRestartRecovery:
    def test_runs_queryable_after_control_plane_restart(
        self, v1_env, client, auth, stub_runner
    ) -> None:
        agent = create_agent(client, auth)["agent"]
        before = wait_run(client, auth, agent["id"], "run-1")
        client.delete(f"/v1/agents/{agent['id']}", headers=auth)

        # Simulated restart: a fresh app over the same session store and a
        # newly opened FileRunStore on the same directory.
        root = v1_env.app.state.run_store.root
        app2 = create_app(
            backend=v1_env.backend,
            store=v1_env.store,
            run_store=FileRunStore(root),
            runner_cmd=[sys.executable, str(stub_runner)],
            keepalive_s=0.2,
        )
        app2.state.account_registry = v1_env.registry
        app2.state.scheduler = v1_env.scheduler
        app2.state.api_key_store = v1_env.keys
        with TestClient(app2) as client2:
            after = _get_run(client2, auth, agent["id"])
            assert after["status"] == "FINISHED"
            for key in ("status", "result", "usage", "error", "finished_at"):
                assert after[key] == before[key], key
            runs = client2.get(f"/v1/agents/{agent['id']}/runs", headers=auth).json()["runs"]
            assert [r["id"] for r in runs] == ["run-1"]


class TestMissingEvidence:
    def test_deleted_record_is_unknown_not_finished(self, client, auth, v1_env) -> None:
        agent = create_agent(client, auth)["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        client.delete(f"/v1/agents/{agent['id']}", headers=auth)
        v1_env.app.state.run_store.delete(agent["id"], 1)

        run = _get_run(client, auth, agent["id"])
        assert run["status"] == "UNKNOWN"
        assert run["error"]["code"] == "evidence_unavailable"

    def test_corrupt_record_is_unknown(self, client, auth, v1_env) -> None:
        agent = create_agent(client, auth)["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        client.delete(f"/v1/agents/{agent['id']}", headers=auth)
        path = v1_env.app.state.run_store.root / agent["id"] / "run-1.json"
        path.write_text("{corrupt", encoding="utf-8")

        run = _get_run(client, auth, agent["id"])
        assert run["status"] == "UNKNOWN"
        assert run["error"]["code"] == "ledger_record_corrupt"

    def test_open_record_finalized_on_agent_close(self, client, auth, v1_env) -> None:
        agent = create_agent(client, auth)["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        # An open record that never reached a terminal write (e.g. the plane
        # died mid-turn) is finalized CANCELLED when the agent is closed.
        v1_env.app.state.run_ledger.begin(agent_id=agent["id"], n=9, status="RUNNING")
        client.delete(f"/v1/agents/{agent['id']}", headers=auth)
        run = _get_run(client, auth, agent["id"], "run-9")
        assert run["status"] == "CANCELLED"


class TestRecordFields:
    def test_run_carries_provider_account_model_and_artifacts(self, client, auth) -> None:
        agent = create_agent(client, auth)["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["provider"] == "codex"
        assert run["account_id"] == "acct-codex-1"
        assert run["model"] == "gpt-5.6-luna"
        assert run["started_at"]
        assert run["finished_at"]
        assert "turns/1.json" in run["artifact_refs"]
        assert "events.jsonl" in run["artifact_refs"]
