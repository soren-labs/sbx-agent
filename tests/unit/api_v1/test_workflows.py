"""Workflow metadata: durable attach, index lookup, scoped cleanup (SOR-84 C1)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from control.api_v1.deps import get_workflow_store
from control.api_v1.state import V1State
from control.api_v1.workflows import WorkflowService
from control.workflow_store import FileWorkflowStore, InMemoryWorkflowStore, WorkflowTaskRecord
from tests.unit.api_v1.conftest import create_agent, wait_run, wait_sandbox, wait_status


def _service(env, *, v1=None, run_states="app"):
    """A WorkflowService over the app's seams (what ``get_workflow_service`` builds)."""
    return WorkflowService(
        env.app.state.workflow_store,
        env.app.state.plane,
        v1=env.app.state.v1_state if v1 is None else v1,
        run_states=env.app.state.run_states if run_states == "app" else run_states,
    )


def _meta(workflow_id: str, task_id: str, role: str = "worker", parent: str | None = None):
    metadata = {"workflow_id": workflow_id, "task_id": task_id, "role": role}
    if parent is not None:
        metadata["parent_task_id"] = parent
    return metadata


class TestAttachOnCreate:
    def test_create_agent_persists_metadata(self, client, auth, v1_env) -> None:
        resp = create_agent(client, auth, metadata=_meta("wf-1", "implement", parent="plan"))
        agent_id = resp["agent"]["id"]
        record = _service(v1_env).for_agent(agent_id)
        assert record is not None
        assert record.owner == v1_env.agents_key_id
        assert record.workflow_id == "wf-1"
        assert record.task_id == "implement"
        assert record.role == "worker"
        assert record.parent_task_id == "plan"
        assert record.created_at

    def test_create_agent_without_metadata_stays_untagged(self, client, auth, v1_env) -> None:
        resp = create_agent(client, auth)
        assert _service(v1_env).for_agent(resp["agent"]["id"]) is None

    def test_metadata_validation_rejects_missing_fields(self, client, auth) -> None:
        for metadata in (
            {"task_id": "t", "role": "worker"},
            {"workflow_id": "wf", "role": "worker"},
            {"workflow_id": "wf", "task_id": "t"},
            {"workflow_id": "", "task_id": "t", "role": "worker"},
        ):
            resp = client.post(
                "/v1/agents",
                json={
                    "prompt": {"text": "hi"},
                    "agent": {"provider": "codex"},
                    "metadata": metadata,
                },
                headers=auth,
            )
            assert resp.status_code == 400, resp.text

    def test_idempotent_replay_does_not_duplicate_index(self, client, auth, v1_env) -> None:
        body = {
            "prompt": {"text": "Create hello.txt"},
            "agent": {"provider": "codex"},
            "metadata": _meta("wf-1", "t-1"),
        }
        headers = {**auth, "Idempotency-Key": "idem-wf-1"}
        first = client.post("/v1/agents", json=body, headers=headers)
        assert first.status_code == 201, first.text
        agent_id = first.json()["agent"]["id"]
        wait_sandbox(v1_env, agent_id)

        # In-memory replay: the stored idempotency entry returns the same
        # agent without touching the index.
        replay = client.post("/v1/agents", json=body, headers=headers)
        assert replay.status_code == 201, replay.text
        assert replay.json()["agent"]["id"] == agent_id
        tasks = v1_env.app.state.workflow_store.list_workflow(v1_env.agents_key_id, "wf-1")
        assert [t.agent_id for t in tasks] == [agent_id]

        # Durable replay (post-restart): a fresh V1State has no idempotency
        # entry, so the route resolves via the durable record and the
        # metadata attach re-runs — still exactly one index entry.
        v1_env.app.state.v1_state = V1State()
        replay = client.post("/v1/agents", json=body, headers=headers)
        assert replay.status_code == 201, replay.text
        assert replay.json()["agent"]["id"] == agent_id
        tasks = v1_env.app.state.workflow_store.list_workflow(v1_env.agents_key_id, "wf-1")
        assert [t.agent_id for t in tasks] == [agent_id]

    def test_attach_requires_core_fields(self, v1_env) -> None:
        svc = _service(v1_env)
        with pytest.raises(ValueError):
            svc.attach(owner="key_x", agent_id="a1", metadata={"workflow_id": "wf"})


class TestLookup:
    def test_lookup_returns_agents_latest_run_and_progress(self, client, auth, v1_env) -> None:
        a1 = create_agent(client, auth, metadata=_meta("wf-1", "impl", role="worker"))
        a2 = create_agent(
            client, auth, metadata=_meta("wf-1", "review", role="reviewer", parent="impl")
        )
        for resp in (a1, a2):
            wait_run(client, auth, resp["agent"]["id"], "run-1")
            # The verdict is durable while the watcher settles its post-run
            # window — the session status flips idle after the checkpoint.
            wait_status(v1_env, resp["agent"]["id"], "idle")

        view = _service(v1_env).lookup(v1_env.agents_key_id, "wf-1")
        assert view is not None
        assert view["workflow_id"] == "wf-1"
        by_id = {a["agent_id"]: a for a in view["agents"]}
        assert set(by_id) == {a1["agent"]["id"], a2["agent"]["id"]}
        worker = by_id[a1["agent"]["id"]]
        assert worker["task_id"] == "impl"
        assert worker["role"] == "worker"
        assert worker["status"] == "idle"
        assert worker["provider"] == "codex"
        assert worker["runs"] == 1
        assert worker["latest_run"]["id"] == "run-1"
        assert worker["latest_run"]["status"] == "FINISHED"
        assert worker["latest_run"]["artifact_refs"]
        reviewer = by_id[a2["agent"]["id"]]
        assert reviewer["parent_task_id"] == "impl"
        progress = view["progress"]
        assert progress["tasks"] == 2
        assert progress["agents"] == 2
        assert progress["open_agents"] == 2
        assert progress["runs"] == 2
        assert progress["latest_runs_by_status"] == {"FINISHED": 2}
        assert progress["all_terminal"] is True

    def test_lookup_reflects_open_run(self, client, auth, v1_env) -> None:
        resp = create_agent(client, auth, metadata=_meta("wf-open", "t-1"))
        agent_id = resp["agent"]["id"]
        view = _service(v1_env).lookup(v1_env.agents_key_id, "wf-open")
        assert view is not None
        (entry,) = view["agents"]
        assert entry["latest_run"]["id"] == "run-1"
        assert entry["latest_run"]["status"] in ("CREATING", "RUNNING", "FINISHED")
        wait_sandbox(v1_env, agent_id)

    def test_lookup_unknown_workflow_is_none(self, v1_env) -> None:
        assert _service(v1_env).lookup(v1_env.agents_key_id, "wf-ghost") is None

    def test_lookup_is_scoped_by_owner(self, client, auth, v1_env) -> None:
        mine = create_agent(client, auth, metadata=_meta("shared-wf", "t-1"))
        other_key, other_token = v1_env.keys.create(label="other", scopes=("agents",))
        other_auth = {"Authorization": f"Bearer {other_token}"}
        theirs = create_agent(client, other_auth, metadata=_meta("shared-wf", "t-9"))

        svc = _service(v1_env)
        mine_view = svc.lookup(v1_env.agents_key_id, "shared-wf")
        theirs_view = svc.lookup(other_key.id, "shared-wf")
        assert [a["agent_id"] for a in mine_view["agents"]] == [mine["agent"]["id"]]
        assert [a["agent_id"] for a in theirs_view["agents"]] == [theirs["agent"]["id"]]

    def test_latest_run_keeps_run_contract_shape(self, client, auth, v1_env) -> None:
        """``latest_run`` is a ``Run`` — required keys incl. ``agent_id``
        and non-null ``created_at``/``updated_at`` stay populated."""
        a = create_agent(client, auth, metadata=_meta("wf-shape", "t-1"))
        wait_run(client, auth, a["agent"]["id"], "run-1")
        view = _service(v1_env).lookup(v1_env.agents_key_id, "wf-shape")
        latest = view["agents"][0]["latest_run"]
        for key in ("id", "agent_id", "status", "created_at", "updated_at"):
            assert latest[key], f"latest_run[{key}] must be populated"
        assert latest["agent_id"] == a["agent"]["id"]

    def test_derived_latest_run_keeps_run_contract_shape(self, v1_env) -> None:
        """Ledger-less fallback: a run derived from the session record still
        satisfies the required ``Run`` fields (status never inferred)."""
        from datetime import UTC, datetime

        from control.store import SessionRecord

        now = datetime.now(UTC)
        rec = SessionRecord(
            id="ag-der",
            title="t",
            status="idle",
            created_at=now,
            updated_at=now,
            model="m",
            turns=1,
            usage=None,
            messages=[],
            owner=v1_env.agents_key_id,
            sandbox_tags={"provider": "codex", "account_id": "acct-1"},
        )
        plane = SimpleNamespace(get=lambda agent_id: rec, run_ledger=None)
        svc = WorkflowService(InMemoryWorkflowStore(), plane, v1=V1State(), run_states=None)
        svc.attach(
            owner=v1_env.agents_key_id,
            agent_id="ag-der",
            metadata={"workflow_id": "wf-der", "task_id": "t", "role": "worker"},
        )
        view = svc.lookup(v1_env.agents_key_id, "wf-der")
        latest = view["agents"][0]["latest_run"]
        assert latest["agent_id"] == "ag-der"
        assert latest["status"] == "UNKNOWN"
        assert latest["created_at"] and latest["updated_at"]
        assert latest["provider"] == "codex" and latest["model"] == "m"
        assert "usage" not in latest  # unmeasured → omitted, never zeros

    def test_metadata_survives_store_reopen(self, client, auth, v1_env, tmp_path) -> None:
        """A fresh store object over the same dir = control-plane restart."""
        v1_env.app.state.workflow_store = FileWorkflowStore(tmp_path / "wf")
        resp = create_agent(client, auth, metadata=_meta("wf-1", "impl"))
        wait_run(client, auth, resp["agent"]["id"], "run-1")

        # New store + new service + no v1/run-state seams: the durable index
        # and run ledger alone recover the workflow.
        v1_env.app.state.workflow_store = FileWorkflowStore(tmp_path / "wf")
        svc = _service(v1_env, v1=V1State(), run_states=None)
        view = svc.lookup(v1_env.agents_key_id, "wf-1")
        assert view is not None
        (entry,) = view["agents"]
        assert entry["agent_id"] == resp["agent"]["id"]
        assert entry["task_id"] == "impl"
        assert entry["latest_run"]["status"] == "FINISHED"


class TestCleanup:
    def test_cleanup_closes_only_scoped_workflow(self, client, auth, v1_env) -> None:
        target = create_agent(client, auth, metadata=_meta("wf-a", "t-1"))
        other_wf = create_agent(client, auth, metadata=_meta("wf-b", "t-1"))
        # Same workflow_id under a different api key must never be touched.
        other_key, other_token = v1_env.keys.create(label="other", scopes=("agents",))
        foreign = create_agent(
            client,
            {"Authorization": f"Bearer {other_token}"},
            metadata=_meta("wf-a", "t-9"),
        )
        for resp in (target, other_wf, foreign):
            wait_sandbox(v1_env, resp["agent"]["id"])

        svc = _service(v1_env)
        result = svc.cleanup(v1_env.agents_key_id, "wf-a")
        assert result is not None
        assert result["matched"] == 1
        assert result["closed"] == [target["agent"]["id"]]
        assert result["skipped"] == [] and result["errors"] == {}
        assert v1_env.store.get(target["agent"]["id"]).status == "closed"
        # Other workflow (same owner) and foreign owner's same-named
        # workflow are untouched.
        assert v1_env.store.get(other_wf["agent"]["id"]).status != "closed"
        assert v1_env.store.get(foreign["agent"]["id"]).status != "closed"

        # Repeat cleanup is a no-op on the already-closed agent.
        again = svc.cleanup(v1_env.agents_key_id, "wf-a")
        assert again["closed"] == []
        assert again["already_terminal"] == [target["agent"]["id"]]
        assert v1_env.store.get(other_wf["agent"]["id"]).status != "closed"
        assert v1_env.store.get(foreign["agent"]["id"]).status != "closed"

    def test_cleanup_closes_every_agent_in_workflow(self, client, auth, v1_env) -> None:
        a1 = create_agent(client, auth, metadata=_meta("wf-multi", "t-1"))
        a2 = create_agent(client, auth, metadata=_meta("wf-multi", "t-2", role="reviewer"))
        ids = sorted(a["agent"]["id"] for a in (a1, a2))
        for resp in (a1, a2):
            wait_run(client, auth, resp["agent"]["id"], "run-1")

        result = _service(v1_env).cleanup(v1_env.agents_key_id, "wf-multi")
        assert sorted(result["closed"]) == ids
        view = _service(v1_env).lookup(v1_env.agents_key_id, "wf-multi")
        assert view["progress"]["open_agents"] == 0
        assert view["progress"]["all_terminal"] is True
        for agent_id in ids:
            assert v1_env.store.get(agent_id).status == "closed"

    def test_cleanup_unknown_workflow_is_none(self, v1_env) -> None:
        assert _service(v1_env).cleanup(v1_env.agents_key_id, "wf-ghost") is None

    def test_cleanup_never_closes_foreign_owned_session(self, client, auth, v1_env) -> None:
        """Even a poisoned index entry cannot make cleanup cross owners."""
        other_key, other_token = v1_env.keys.create(label="other", scopes=("agents",))
        foreign = create_agent(
            client,
            {"Authorization": f"Bearer {other_token}"},
            metadata=_meta("wf-x", "t-1"),
        )
        wait_sandbox(v1_env, foreign["agent"]["id"])
        foreign_id = foreign["agent"]["id"]

        # Simulate a corrupt/malicious index: the foreign agent lands inside
        # key A's workflow index while its session owner stays key B.
        store = v1_env.app.state.workflow_store
        store.attach(
            WorkflowTaskRecord(
                owner=v1_env.agents_key_id,
                workflow_id="wf-poison",
                task_id="t-evil",
                role="worker",
                agent_id=foreign_id,
            )
        )
        result = _service(v1_env).cleanup(v1_env.agents_key_id, "wf-poison")
        assert result["skipped"] == [foreign_id]
        assert result["closed"] == []
        assert v1_env.store.get(foreign_id).status != "closed"
        assert other_key.id != v1_env.agents_key_id

    def test_cleanup_reports_missing_session(self, client, auth, v1_env) -> None:
        resp = create_agent(client, auth, metadata=_meta("wf-m", "t-1"))
        agent_id = resp["agent"]["id"]
        wait_sandbox(v1_env, agent_id)
        v1_env.store.delete(agent_id)
        result = _service(v1_env).cleanup(v1_env.agents_key_id, "wf-m")
        assert result["missing"] == [agent_id]
        assert result["closed"] == []


class TestSeamWiring:
    def test_v1_state_fallback_store(self, v1_env) -> None:
        """Without ``app.state.workflow_store`` the V1State fallback serves."""
        original = v1_env.app.state.workflow_store
        req = SimpleNamespace(app=v1_env.app)
        try:
            del v1_env.app.state.workflow_store
            store = get_workflow_store(req)
            assert isinstance(store, InMemoryWorkflowStore)
            assert store is v1_env.app.state.v1_state.workflows
        finally:
            v1_env.app.state.workflow_store = original
