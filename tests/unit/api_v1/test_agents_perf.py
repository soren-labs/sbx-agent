"""SOR-200: ``GET /v1/agents`` performance coverage at 60+/100+ agents.

Regression guard for the workflow-Dict N+1: the list route must filter,
sort and page on record-level fields *before* enrichment, and resolve the
page's ``metadata`` echoes in one batched store pass — never a serial
per-agent read. The counting store instruments the primitive reads a
``modal.Dict`` backend would charge one network round-trip each for, so
the op-count asserts hold for production backends too.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from control.store import SessionRecord
from control.workflow_store import InMemoryWorkflowStore, WorkflowTaskRecord

PAGE_SIZE = 100
LOCAL_P95_BUDGET_S = 1.5


class CountingWorkflowStore(InMemoryWorkflowStore):
    """Instrument the primitive reads a Modal Dict bills as round-trips."""

    def __init__(self) -> None:
        super().__init__()
        self.agent_gets = 0
        self.agent_scans = 0
        self.index_gets = 0

    def _get_agent_raw(self, agent_id: str):
        self.agent_gets += 1
        return super()._get_agent_raw(agent_id)

    def _iter_agent_raws(self):
        self.agent_scans += 1
        return super()._iter_agent_raws()

    def _get_index_raw(self, owner: str, workflow_id: str):
        self.index_gets += 1
        return super()._get_index_raw(owner, workflow_id)

    def reset(self) -> None:
        self.agent_gets = 0
        self.agent_scans = 0
        self.index_gets = 0


def _install_counting_store(v1_env) -> CountingWorkflowStore:
    store = CountingWorkflowStore()
    v1_env.app.state.workflow_store = store
    return store


def _seed_agent(
    v1_env,
    i: int,
    *,
    owner: str,
    status: str = "idle",
    provider: str = "codex",
    account_id: str = "acct-1",
) -> str:
    """One durable session record, created_at-ordered by index."""
    agent_id = f"ag-{i:04d}"
    created = datetime.now(UTC) - timedelta(minutes=10_000 - i)
    v1_env.store.put(
        SessionRecord(
            id=agent_id,
            title=f"agent-{i}",
            status=status,
            created_at=created,
            updated_at=created,
            model="gpt-5.6-luna",
            turns=1,
            usage=None,
            messages=[],
            owner=owner,
            sandbox_tags={"provider": provider, "account_id": account_id},
        )
    )
    return agent_id


def _bind(
    store: InMemoryWorkflowStore, owner: str, agent_id: str, workflow_id: str, i: int
) -> None:
    store.attach(
        WorkflowTaskRecord(
            owner=owner,
            workflow_id=workflow_id,
            task_id=f"task-{i}",
            role="worker",
            agent_id=agent_id,
        )
    )


class TestBatchedEnrichment:
    def test_60_bound_agents_one_store_pass(self, client, auth, v1_env) -> None:
        store = _install_counting_store(v1_env)
        ids = [_seed_agent(v1_env, i, owner=v1_env.agents_key_id) for i in range(60)]
        for i, agent_id in enumerate(ids):
            _bind(store, v1_env.agents_key_id, agent_id, "wf-perf", i)

        store.reset()
        start = time.monotonic()
        resp = client.get("/v1/agents", headers=auth)
        elapsed = time.monotonic() - start
        assert resp.status_code == 200
        agents = resp.json()["agents"]
        assert len(agents) == 60
        assert [a["id"] for a in agents] == sorted(ids)
        for i, agent in enumerate(agents):
            assert agent["metadata"] == {
                "workflow_id": "wf-perf",
                "task_id": f"task-{i}",
                "role": "worker",
                "parent_task_id": None,
            }
        # The whole page resolved without a single serial per-agent read:
        # one batched scan, zero per-agent gets.
        assert store.agent_gets == 0
        assert store.agent_scans <= 1
        # Local bound mirroring the Modal P95 <1.5s target.
        assert elapsed < LOCAL_P95_BUDGET_S

    def test_120_agents_paginate_before_enrichment(self, client, auth, v1_env) -> None:
        store = _install_counting_store(v1_env)
        ids = [
            _seed_agent(
                v1_env,
                i,
                owner=v1_env.agents_key_id,
                status="closed" if i % 7 == 0 else "idle",
                provider="devin" if i % 3 == 0 else "codex",
                account_id=f"acct-{i % 4}",
            )
            for i in range(120)
        ]
        # 80 bound across two workflows, 40 unbound.
        for i, agent_id in enumerate(ids[:80]):
            _bind(store, v1_env.agents_key_id, agent_id, f"wf-{i % 2}", i)

        store.reset()
        first = client.get("/v1/agents", headers=auth)
        assert first.status_code == 200
        body = first.json()
        assert len(body["agents"]) == PAGE_SIZE
        assert body["next_cursor"] == str(PAGE_SIZE)
        # Ordering is by created_at: agents sort by id ascending here.
        assert all(a["metadata"] is not None for a in body["agents"][:80])
        assert all(a["metadata"] is None for a in body["agents"][80:])

        second = client.get(f"/v1/agents?cursor={body['next_cursor']}", headers=auth)
        body2 = second.json()
        assert len(body2["agents"]) == 20
        assert body2["next_cursor"] is None
        ordered = [a["id"] for a in body["agents"]] + [a["id"] for a in body2["agents"]]
        assert ordered == sorted(ids)
        # Both pages together: zero serial per-agent gets, one scan each.
        assert store.agent_gets == 0
        assert store.agent_scans <= 2

    def test_filters_apply_before_enrichment(self, client, auth, v1_env) -> None:
        store = _install_counting_store(v1_env)
        ids = [
            _seed_agent(
                v1_env,
                i,
                owner=v1_env.agents_key_id,
                status="closed" if i % 2 else "idle",
                provider="devin" if i % 3 == 0 else "codex",
                account_id=f"acct-{i % 4}",
            )
            for i in range(100)
        ]
        for i, agent_id in enumerate(ids):
            _bind(store, v1_env.agents_key_id, agent_id, "wf-f", i)

        store.reset()
        devin = client.get("/v1/agents?provider=devin", headers=auth).json()
        assert devin["agents"] and all(a["provider"] == "devin" for a in devin["agents"])
        closed = client.get("/v1/agents?status=closed", headers=auth).json()
        assert closed["agents"] and all(a["status"] == "closed" for a in closed["agents"])
        acct = client.get("/v1/agents?account_id=acct-2", headers=auth).json()
        assert acct["agents"] and all(a["account_id"] == "acct-2" for a in acct["agents"])
        scoped = client.get("/v1/agents?workflow_id=wf-f", headers=auth).json()
        assert len(scoped["agents"]) == 100
        assert all(a["metadata"]["workflow_id"] == "wf-f" for a in scoped["agents"])
        # Filtered/scoped lists never degenerate to per-agent reads either.
        assert store.agent_gets == 0

    def test_batch_echo_matches_serial_lookup(self, client, auth, v1_env) -> None:
        """Batched ``all_bindings`` echoes exactly what serial ``for_agent`` did."""
        store = _install_counting_store(v1_env)
        for i in range(80):
            agent_id = _seed_agent(v1_env, i, owner=v1_env.agents_key_id)
            if i % 5:
                _bind(store, v1_env.agents_key_id, agent_id, "wf-echo", i)

        agents = client.get("/v1/agents", headers=auth).json()["agents"]
        assert len(agents) == 80
        for agent in agents:
            serial = store.for_agent(agent["id"])
            expected = (
                {
                    "workflow_id": serial.workflow_id,
                    "task_id": serial.task_id,
                    "role": serial.role,
                    "parent_task_id": serial.parent_task_id,
                }
                if serial is not None
                else None
            )
            assert agent["metadata"] == expected


class TestAllBindings:
    def test_all_bindings_matches_for_agent(self) -> None:
        store = InMemoryWorkflowStore()
        for i in range(5):
            _bind(store, "key_a", f"ag-{i}", "wf-1", i)
        got = store.all_bindings()
        assert set(got) == {f"ag-{i}" for i in range(5)}
        assert got["ag-3"] == store.for_agent("ag-3")
