"""SOR-202: ``GET /v1/agents/summary`` — the cheap rollup the Console polls.

The sidebar badge and the gated agents-list refresh hit this instead of a
full ``GET /v1/agents`` page: one store pass, no per-agent payload build,
and — unless ``workflow_id`` is supplied — no workflow-binding reads.
``version`` (``{total}:{max updated_at}``) must move whenever the
filtered set mutates so clients only refetch the expensive page on a real
change.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from control.api_v1.workflows import WorkflowService
from control.store import SessionRecord
from control.workflow_store import InMemoryWorkflowStore


class CountingWorkflowStore(InMemoryWorkflowStore):
    """Fails loudly if the summary reads workflow bindings unfiltered."""

    def __init__(self) -> None:
        super().__init__()
        self.scans = 0
        self.scope_reads = 0

    def all_bindings(self):
        self.scans += 1
        return super().all_bindings()

    def list_workflow(self, owner: str, workflow_id: str):
        self.scope_reads += 1
        return super().list_workflow(owner, workflow_id)


def _seed(
    env,
    agent_id: str,
    *,
    status: str = "idle",
    provider: str = "codex",
    account_id: str = "acct-1",
    minutes_ago: int = 0,
) -> None:
    stamp = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    env.store.put(
        SessionRecord(
            id=agent_id,
            title=agent_id,
            status=status,
            created_at=stamp,
            updated_at=stamp,
            model="gpt-5.6-luna",
            turns=0,
            usage=None,
            messages=[],
            owner="owner-1",
            sandbox_tags={"provider": provider, "account_id": account_id},
        )
    )


def test_summary_counts_statuses_and_live(client, auth, v1_env) -> None:
    _seed(v1_env, "ag-idle-1")
    _seed(v1_env, "ag-idle-2")
    _seed(v1_env, "ag-run", status="running")
    _seed(v1_env, "ag-new", status="creating")
    _seed(v1_env, "ag-dead", status="closed")
    _seed(v1_env, "ag-susp", status="suspended")  # surfaces as idle

    resp = client.get("/v1/agents/summary", headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 6
    assert body["live"] == 5  # creating + idle(+suspended) + running
    assert body["by_status"] == {"idle": 3, "running": 1, "creating": 1, "closed": 1}
    assert body["version"].startswith("6:")


def test_summary_version_tracks_mutations(client, auth, v1_env) -> None:
    _seed(v1_env, "ag-1")
    first = client.get("/v1/agents/summary", headers=auth).json()["version"]

    _seed(v1_env, "ag-2")
    second = client.get("/v1/agents/summary", headers=auth).json()["version"]
    assert second != first

    # A status/heartbeat update on an existing record also moves it.
    rec = v1_env.store.get("ag-1")
    rec.status = "running"
    rec.updated_at = datetime.now(UTC) + timedelta(seconds=1)
    v1_env.store.put(rec)
    third = client.get("/v1/agents/summary", headers=auth).json()["version"]
    assert third != second


def test_summary_filters(client, auth, v1_env) -> None:
    _seed(v1_env, "ag-codex", provider="codex")
    _seed(v1_env, "ag-grok", provider="grok", status="running")
    _seed(v1_env, "ag-grok-2", provider="grok", status="closed")

    body = client.get("/v1/agents/summary", params={"provider": "grok"}, headers=auth).json()
    assert body["total"] == 2
    assert body["by_status"] == {"running": 1, "closed": 1}

    body = client.get(
        "/v1/agents/summary", params={"provider": "grok", "status": "running"}, headers=auth
    ).json()
    assert body["total"] == 1
    assert body["live"] == 1

    body = client.get("/v1/agents/summary", params={"account_id": "acct-9"}, headers=auth).json()
    assert body["total"] == 0
    assert body["by_status"] == {}


def test_summary_workflow_scope(client, auth, v1_env) -> None:
    svc = WorkflowService(v1_env.app.state.workflow_store, v1_env.app.state.plane)
    svc.attach(
        owner=v1_env.agents_key_id,
        agent_id="ag-bound",
        metadata={"workflow_id": "wf-1", "task_id": "impl", "role": "worker"},
    )
    _seed(v1_env, "ag-bound")
    _seed(v1_env, "ag-free")

    body = client.get("/v1/agents/summary", params={"workflow_id": "wf-1"}, headers=auth).json()
    assert body["total"] == 1
    assert body["by_status"] == {"idle": 1}


def test_summary_skips_binding_reads_unfiltered(client, auth, v1_env) -> None:
    """Unfiltered summary must stay cheap: no all-bindings store pass."""
    store = CountingWorkflowStore()
    v1_env.app.state.workflow_store = store
    _seed(v1_env, "ag-1")
    resp = client.get("/v1/agents/summary", headers=auth)
    assert resp.status_code == 200
    assert store.scans == 0
    assert store.scope_reads == 0
