"""SOR-84 integration: /v1 workflow routes + SDK recover/watch surfaces.

* ``GET /v1/workflows/{id}`` — recover a workflow from api key + id alone.
* ``GET /v1/agents?workflow_id=`` + ``agent.metadata`` — the surface the
  SDK ``recover`` composes over (strict ``metadata.workflow_id`` filter).
* ``DELETE /v1/workflows/{id}`` — scoped, idempotent cleanup.
* Usage honesty — unmeasured usage is ``null`` (unavailable), never zeros.
* ``SbxClient`` ``recover`` / ``watch`` / ``resume`` / ``wait_many`` against
  a live server (``live_base``); SSE resume keeps opaque event ids and
  replays without duplicate side effects.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from control.store import SessionRecord
from tests.unit.api_v1.conftest import create_agent, wait_run, wait_sandbox

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.sbx_client import SbxClient  # noqa: E402


def _meta(workflow_id: str, task_id: str, role: str = "worker", parent: str | None = None):
    metadata = {"workflow_id": workflow_id, "task_id": task_id, "role": role}
    if parent is not None:
        metadata["parent_task_id"] = parent
    return metadata


def _lift_cap(v1_env, n: int = 16) -> None:
    """Raise the per-key concurrent sandbox cap for multi-agent tests."""
    v1_env.app.state.plane.max_concurrent = n


class TestWorkflowQueryRoute:
    def test_workflow_view_recovers_agents_latest_runs_progress(self, client, auth, v1_env) -> None:
        _lift_cap(v1_env)
        a1 = create_agent(client, auth, metadata=_meta("wf-q", "impl"))
        a2 = create_agent(
            client,
            auth,
            metadata=_meta("wf-q", "review", role="reviewer", parent="impl"),
        )
        other = create_agent(client, auth, metadata=_meta("wf-other", "t"))
        for resp in (a1, a2):
            wait_run(client, auth, resp["agent"]["id"], "run-1")
        wait_sandbox(v1_env, other["agent"]["id"])

        resp = client.get("/v1/workflows/wf-q", headers=auth)
        assert resp.status_code == 200
        view = resp.json()
        assert view["workflow_id"] == "wf-q"
        by_id = {a["agent_id"]: a for a in view["agents"]}
        assert set(by_id) == {a1["agent"]["id"], a2["agent"]["id"]}
        assert by_id[a1["agent"]["id"]]["latest_run"]["status"] == "FINISHED"
        assert by_id[a1["agent"]["id"]]["latest_run"]["artifact_refs"]
        assert by_id[a2["agent"]["id"]]["role"] == "reviewer"
        assert by_id[a2["agent"]["id"]]["parent_task_id"] == "impl"
        progress = view["progress"]
        assert progress["tasks"] == 2
        assert progress["agents"] == 2
        assert progress["latest_runs_by_status"]["FINISHED"] == 2
        assert progress["all_terminal"] is True

    def test_unknown_and_foreign_workflow_is_404(self, client, auth, v1_env) -> None:
        assert client.get("/v1/workflows/ghost", headers=auth).status_code == 404
        create_agent(client, auth, metadata=_meta("wf-hidden", "t"))
        _, other_token = v1_env.keys.create(label="other", scopes=("agents",))
        resp = client.get(
            "/v1/workflows/wf-hidden", headers={"Authorization": f"Bearer {other_token}"}
        )
        assert resp.status_code == 404
        assert client.get("/v1/workflows/wf-hidden").status_code == 401

    def test_followup_metadata_rebinds_task(self, client, auth, v1_env) -> None:
        a = create_agent(client, auth, metadata=_meta("wf-fu", "t-1"))
        wait_run(client, auth, a["agent"]["id"], "run-1")
        resp = client.post(
            f"/v1/agents/{a['agent']['id']}/runs",
            json={"prompt": {"text": "second turn"}, "metadata": _meta("wf-fu", "t-2")},
            headers=auth,
        )
        assert resp.status_code == 201, resp.text
        record = v1_env.app.state.workflow_store.for_agent(a["agent"]["id"])
        assert record is not None
        assert record.workflow_id == "wf-fu"
        assert record.task_id == "t-2"  # re-bound by the follow-up

    def test_reaped_agent_open_run_reports_terminal_not_running(self, client, auth, v1_env) -> None:
        """The reaper marks a session terminal without finalizing its open
        ledger runs; the workflow view must derive the same honest terminal
        status the run route reports — not claim RUNNING forever."""
        agent = create_agent(client, auth, metadata=_meta("wf-reap", "t-1"))["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        # Simulate a reaper pass mid-run-2: the session record goes terminal
        # while the ledger still holds an open record for run-2.
        v1_env.app.state.run_states.begin(agent["id"], 2, prompt="lost turn", status="RUNNING")
        rec = v1_env.store.get(agent["id"])
        rec.status = "timed_out"
        rec.ended_at = datetime.now(UTC)
        rec.updated_at = rec.ended_at
        v1_env.store.put(rec)

        view = client.get("/v1/workflows/wf-reap", headers=auth).json()
        (entry,) = [a for a in view["agents"] if a["agent_id"] == agent["id"]]
        latest = entry["latest_run"]
        assert latest["id"] == "run-2"
        assert latest["status"] == "EXPIRED"
        assert latest["error"]["code"] == "timeout"
        assert view["progress"]["all_terminal"] is True
        # The run route and the workflow view agree.
        run = client.get(f"/v1/agents/{agent['id']}/runs/run-2", headers=auth).json()
        assert run["status"] == "EXPIRED"


class TestWorkflowCleanupRoute:
    def test_scoped_cleanup_only_target_workflow(self, client, auth, v1_env) -> None:
        _lift_cap(v1_env)
        target = create_agent(client, auth, metadata=_meta("wf-del", "t-1"))
        sibling = create_agent(client, auth, metadata=_meta("wf-del", "t-2"))
        other = create_agent(client, auth, metadata=_meta("wf-keep", "t-1"))
        untagged = create_agent(client, auth)
        _, other_token = v1_env.keys.create(label="other", scopes=("agents",))
        foreign = create_agent(
            client,
            {"Authorization": f"Bearer {other_token}"},
            metadata=_meta("wf-del", "t-9"),
        )
        for resp in (target, sibling, other, untagged, foreign):
            wait_sandbox(v1_env, resp["agent"]["id"])

        resp = client.delete("/v1/workflows/wf-del", headers=auth)
        assert resp.status_code == 200
        result = resp.json()
        assert result["matched"] == 2
        assert sorted(result["closed"]) == sorted([target["agent"]["id"], sibling["agent"]["id"]])
        assert result["skipped"] == [] and result["errors"] == {}
        assert v1_env.store.get(other["agent"]["id"]).status != "closed"
        assert v1_env.store.get(untagged["agent"]["id"]).status != "closed"
        # Another principal's same-named workflow is never touched.
        assert v1_env.store.get(foreign["agent"]["id"]).status != "closed"

        # Idempotent: a second cleanup closes nothing.
        again = client.delete("/v1/workflows/wf-del", headers=auth).json()
        assert again["closed"] == []
        assert sorted(again["already_terminal"]) == sorted(
            [target["agent"]["id"], sibling["agent"]["id"]]
        )
        assert v1_env.store.get(other["agent"]["id"]).status != "closed"

        # The workflow view still resolves — cleanup closes agents, not the
        # durable record.
        view = client.get("/v1/workflows/wf-del", headers=auth).json()
        assert view["progress"]["open_agents"] == 0

    def test_cleanup_unknown_workflow_is_404(self, client, auth) -> None:
        assert client.delete("/v1/workflows/ghost", headers=auth).status_code == 404
        assert client.delete("/v1/workflows/ghost").status_code == 401


class TestAgentsWorkflowScope:
    def test_workflow_id_filter_and_metadata_payload(self, client, auth, v1_env) -> None:
        _lift_cap(v1_env)
        a = create_agent(client, auth, metadata=_meta("wf-ls", "impl", parent="plan"))
        create_agent(client, auth, metadata=_meta("wf-other", "t"))
        create_agent(client, auth)
        wait_sandbox(v1_env, a["agent"]["id"])

        resp = client.get("/v1/agents?workflow_id=wf-ls", headers=auth)
        assert resp.status_code == 200
        agents = resp.json()["agents"]
        assert [x["id"] for x in agents] == [a["agent"]["id"]]
        assert agents[0]["metadata"] == {
            "workflow_id": "wf-ls",
            "task_id": "impl",
            "role": "worker",
            "parent_task_id": "plan",
        }

        unscoped = client.get("/v1/agents", headers=auth).json()["agents"]
        by_id = {x["id"]: x for x in unscoped}
        assert by_id[a["agent"]["id"]]["metadata"]["workflow_id"] == "wf-ls"

        # The filter is owner-scoped: another key's view of the same
        # workflow_id is empty.
        _, other_token = v1_env.keys.create(label="other", scopes=("agents",))
        foreign = client.get(
            "/v1/agents?workflow_id=wf-ls",
            headers={"Authorization": f"Bearer {other_token}"},
        ).json()
        assert foreign["agents"] == []

    def test_get_agent_echoes_metadata(self, client, auth, v1_env) -> None:
        a = create_agent(client, auth, metadata=_meta("wf-g", "impl"))
        detail = client.get(f"/v1/agents/{a['agent']['id']}", headers=auth).json()
        assert detail["metadata"]["workflow_id"] == "wf-g"
        assert detail["metadata"]["task_id"] == "impl"
        wait_sandbox(v1_env, a["agent"]["id"])


class TestUsageUnavailable:
    def test_unmeasured_usage_is_null_not_zeros(self, client, auth, v1_env) -> None:
        """A session that never measured usage reports ``null``."""
        now = datetime.now(UTC)
        v1_env.store.put(
            SessionRecord(
                id="ag-raw",
                title="raw",
                status="idle",
                created_at=now,
                updated_at=now,
                model="m",
                turns=0,
                usage=None,
                messages=[],
                owner=v1_env.agents_key_id,
            )
        )

        detail = client.get("/v1/agents/ag-raw", headers=auth).json()
        assert detail["usage"] is None
        body = client.get("/v1/agents/ag-raw/usage", headers=auth).json()
        assert body["usage"] is None
        # sandbox_seconds / cost estimate stay measured wall-clock values.
        assert body["sandbox_seconds"] >= 0
        assert body["cost_estimate_usd"] >= 0

    def test_measured_usage_reports_dict(self, client, auth) -> None:
        agent = create_agent(client, auth)["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        body = client.get(f"/v1/agents/{agent['id']}/usage", headers=auth).json()
        assert isinstance(body["usage"], dict)
        assert body["usage"]["input_tokens"] >= 0
        detail = client.get(f"/v1/agents/{agent['id']}", headers=auth).json()
        assert isinstance(detail["usage"], dict)


class TestSdkOrchestration:
    """SOR-84 acceptance: fresh-client recover + watch/resume + wait_many."""

    def test_fresh_client_recovers_wait_many_and_cleans_up(
        self, client, auth, live_base, v1_env
    ) -> None:
        _lift_cap(v1_env)
        a1 = create_agent(client, auth, metadata=_meta("wf-sdk", "impl"))
        a2 = create_agent(client, auth, metadata=_meta("wf-sdk", "review", role="reviewer"))
        a3 = create_agent(client, auth, metadata=_meta("wf-other", "t"))
        a4 = create_agent(client, auth)
        for resp in (a1, a2, a3, a4):
            wait_run(client, auth, resp["agent"]["id"], "run-1")

        # A brand-new client process: only API key + workflow_id.
        fresh = SbxClient(base_url=live_base, api_key=v1_env.agents_token)
        try:
            rec = fresh.recover("wf-sdk")
            assert sorted(a["id"] for a in rec.agents) == sorted(
                [a1["agent"]["id"], a2["agent"]["id"]]
            )
            assert all(r["status"] == "FINISHED" for r in rec.latest_runs.values())
            assert all(refs for refs in rec.artifact_refs().values())

            results = fresh.wait_many(rec.handles(), poll_s=0.1)
            assert set(results) == set(rec.handles())
            assert all(r["status"] == "FINISHED" for r in results.values())

            closed = fresh.close_workflow("wf-sdk")
            assert sorted(c["id"] for c in closed) == sorted([a1["agent"]["id"], a2["agent"]["id"]])
            for other in (a3, a4):
                assert fresh.get_agent(other["agent"]["id"])["status"] != "closed"
        finally:
            fresh.close()

    def test_wait_many_mixed_terminal_outcomes(
        self, client, auth, live_base, v1_env, monkeypatch
    ) -> None:
        _lift_cap(v1_env)
        # Scenario flips are serialized: each run reaches terminal before the
        # next create, so the env a turn exec observes is deterministic.
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "nonzero")
        bad = create_agent(client, auth, metadata=_meta("wf-mix", "bad"))
        assert wait_run(client, auth, bad["agent"]["id"], "run-1")["status"] == "ERROR"

        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
        good = create_agent(client, auth, metadata=_meta("wf-mix", "good"))
        assert wait_run(client, auth, good["agent"]["id"], "run-1")["status"] == "FINISHED"

        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "hang")
        hung = create_agent(client, auth, metadata=_meta("wf-mix", "hung"))
        deadline = time.monotonic() + 15
        while True:
            run = client.get(f"/v1/agents/{hung['agent']['id']}/runs/run-1", headers=auth).json()
            if run["status"] == "RUNNING":
                break
            assert time.monotonic() < deadline, run
            time.sleep(0.1)
        cancelled = client.post(f"/v1/agents/{hung['agent']['id']}/runs/run-1/cancel", headers=auth)
        assert cancelled.json()["status"] == "CANCELLED"

        sbx = SbxClient(base_url=live_base, api_key=v1_env.agents_token)
        try:
            rec = sbx.recover("wf-mix")
            by_task = {a["metadata"]["task_id"]: a["id"] for a in rec.agents}
            results = sbx.wait_many(rec.handles(), poll_s=0.1, timeout_s=15)
            assert results[(by_task["bad"], "run-1")]["status"] == "ERROR"
            assert results[(by_task["good"], "run-1")]["status"] == "FINISHED"
            assert results[(by_task["hung"], "run-1")]["status"] == "CANCELLED"
        finally:
            sbx.close()

    def test_sse_resume_keeps_opaque_ids_without_duplicate_side_effects(
        self, client, auth, live_base, v1_env
    ) -> None:
        agent = create_agent(client, auth, metadata=_meta("wf-sse", "t-1"))["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        sbx = SbxClient(base_url=live_base, api_key=v1_env.agents_token)
        try:
            first = []
            for event in sbx.stream_run(agent["id"], "run-1"):
                first.append(event)
                if event.type == "sbx.turn_finished":
                    break
            ids = [e.id for e in first]
            assert len(ids) >= 3 and all(ids)

            pivot = ids[1]
            resumed = []
            for event in sbx.stream_run(agent["id"], "run-1", last_event_id=pivot):
                resumed.append(event)
                if event.type == "sbx.turn_finished":
                    break
            assert resumed
            assert min(int(e.id) for e in resumed) > int(pivot)
            # Nothing already delivered replays across the resume boundary.
            seen = {e.id for e in first if int(e.id) <= int(pivot)}
            assert not (seen & {e.id for e in resumed})

            # resume() re-attaches by workflow discovery — still exactly one
            # run, no side effect dispatched.
            events = list(sbx.resume(agent["id"]))
            assert events[-1].type == "sbx.turn_finished"
            assert len(sbx.list_runs(agent["id"])) == 1
        finally:
            sbx.close()
