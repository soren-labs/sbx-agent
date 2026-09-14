"""Run endpoints: follow-up runs, status derivation, conflicts, cancel."""

from __future__ import annotations

from tests.unit.api_v1.conftest import create_agent, wait_run


class TestRuns:
    def test_followup_run_finishes(self, client, auth) -> None:
        agent = create_agent(client, auth)["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        resp = client.post(
            f"/v1/agents/{agent['id']}/runs",
            json={"prompt": {"text": "append a line"}},
            headers=auth,
        )
        assert resp.status_code == 201
        run = resp.json()
        assert run["id"] == "run-2"
        assert run["agent_id"] == agent["id"]
        assert run["status"] in ("RUNNING", "FINISHED")
        run = wait_run(client, auth, agent["id"], "run-2")
        assert run["status"] == "FINISHED"
        assert run["result"]["text"]

    def test_run_result_text_is_last_agent_message(self, client, auth) -> None:
        agent = create_agent(client, auth)["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "FINISHED"
        # stub_runner success fixture ends with this agent_message
        assert "hello" in run["result"]["text"].lower()

    def test_list_runs(self, client, auth) -> None:
        agent = create_agent(client, auth)["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        client.post(
            f"/v1/agents/{agent['id']}/runs",
            json={"prompt": {"text": "more"}},
            headers=auth,
        )
        wait_run(client, auth, agent["id"], "run-2")
        resp = client.get(f"/v1/agents/{agent['id']}/runs", headers=auth)
        assert resp.status_code == 200
        runs = resp.json()["runs"]
        assert [r["id"] for r in runs] == ["run-1", "run-2"]
        assert all(r["status"] == "FINISHED" for r in runs)

    def test_get_missing_run_is_404(self, client, auth) -> None:
        agent = create_agent(client, auth)["agent"]
        resp = client.get(f"/v1/agents/{agent['id']}/runs/run-9", headers=auth)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"
        resp = client.get(f"/v1/agents/{agent['id']}/runs/not-a-run", headers=auth)
        assert resp.status_code == 404

    def test_run_on_missing_agent_is_404(self, client, auth) -> None:
        resp = client.post(
            "/v1/agents/nope/runs",
            json={"prompt": {"text": "hi"}},
            headers=auth,
        )
        assert resp.status_code == 404

    def test_turn_in_progress_is_409(self, client, auth, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
        monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "30")
        agent = create_agent(client, auth)["agent"]
        resp = client.post(
            f"/v1/agents/{agent['id']}/runs",
            json={"prompt": {"text": "concurrent"}},
            headers=auth,
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "turn_in_progress"
        # cleanup: cancel the in-flight run
        client.post(f"/v1/agents/{agent['id']}/runs/run-1/cancel", headers=auth)

    def test_run_on_closed_agent_is_409(self, client, auth) -> None:
        agent = create_agent(client, auth)["agent"]
        client.delete(f"/v1/agents/{agent['id']}", headers=auth)
        resp = client.post(
            f"/v1/agents/{agent['id']}/runs",
            json={"prompt": {"text": "hi"}},
            headers=auth,
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "session_not_runnable"


class TestCancel:
    def test_cancel_running_run(self, client, auth, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
        monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "30")
        agent = create_agent(client, auth)["agent"]
        resp = client.post(f"/v1/agents/{agent['id']}/runs/run-1/cancel", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["status"] == "CANCELLED"
        # stays cancelled on subsequent reads
        run = client.get(f"/v1/agents/{agent['id']}/runs/run-1", headers=auth).json()
        assert run["status"] == "CANCELLED"

    def test_cancel_terminal_run_returns_it(self, client, auth) -> None:
        agent = create_agent(client, auth)["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        resp = client.post(f"/v1/agents/{agent['id']}/runs/run-1/cancel", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["status"] == "FINISHED"

    def test_cancel_unknown_run_is_404(self, client, auth) -> None:
        agent = create_agent(client, auth)["agent"]
        resp = client.post(f"/v1/agents/{agent['id']}/runs/run-7/cancel", headers=auth)
        assert resp.status_code == 404

    def test_error_run_maps_error_status(self, client, auth, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "nonzero")
        agent = create_agent(client, auth)["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "ERROR"
