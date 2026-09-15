"""Durable workflow/task metadata index: attach, scoping, re-open, corrupt."""

from __future__ import annotations

import json

import pytest
from control.workflow_store import (
    FileWorkflowStore,
    InMemoryWorkflowStore,
    WorkflowTaskRecord,
    record_from_dict,
)


def _record(
    agent_id: str,
    *,
    owner: str = "key_a",
    workflow_id: str = "wf-1",
    task_id: str = "task-1",
    role: str = "worker",
    parent_task_id: str | None = None,
    created_at: str = "",
) -> WorkflowTaskRecord:
    return WorkflowTaskRecord(
        owner=owner,
        workflow_id=workflow_id,
        task_id=task_id,
        role=role,
        agent_id=agent_id,
        parent_task_id=parent_task_id,
        created_at=created_at,
    )


def _stores(tmp_path):
    return [InMemoryWorkflowStore(), FileWorkflowStore(tmp_path / "wf")]


class TestAttach:
    @pytest.mark.parametrize("store_kind", ["memory", "file"])
    def test_attach_roundtrip(self, tmp_path, store_kind) -> None:
        store = _stores(tmp_path)[store_kind == "file"]
        record = store.attach(
            _record("agent-1", task_id="implement", role="worker", parent_task_id="plan")
        )
        assert record.created_at and record.updated_at
        got = store.for_agent("agent-1")
        assert got is not None
        assert got.owner == "key_a"
        assert got.workflow_id == "wf-1"
        assert got.task_id == "implement"
        assert got.role == "worker"
        assert got.parent_task_id == "plan"
        tasks = store.list_workflow("key_a", "wf-1")
        assert [t.agent_id for t in tasks] == ["agent-1"]

    @pytest.mark.parametrize("store_kind", ["memory", "file"])
    def test_reattach_is_idempotent_and_keeps_created_at(self, tmp_path, store_kind) -> None:
        store = _stores(tmp_path)[store_kind == "file"]
        first = store.attach(_record("agent-1", role="worker"))
        again = store.attach(_record("agent-1", role="reviewer"))
        assert again.created_at == first.created_at
        assert again.role == "reviewer"
        assert [t.agent_id for t in store.list_workflow("key_a", "wf-1")] == ["agent-1"]

    @pytest.mark.parametrize("store_kind", ["memory", "file"])
    def test_reattach_other_workflow_moves_index_entry(self, tmp_path, store_kind) -> None:
        store = _stores(tmp_path)[store_kind == "file"]
        store.attach(_record("agent-1", workflow_id="wf-old"))
        store.attach(_record("agent-1", workflow_id="wf-new"))
        assert store.list_workflow("key_a", "wf-old") == []
        assert [t.workflow_id for t in store.list_workflow("key_a", "wf-new")] == ["wf-new"]
        assert store.for_agent("agent-1").workflow_id == "wf-new"

    @pytest.mark.parametrize("store_kind", ["memory", "file"])
    def test_same_workflow_id_is_isolated_per_owner(self, tmp_path, store_kind) -> None:
        store = _stores(tmp_path)[store_kind == "file"]
        store.attach(_record("agent-a", owner="key_a"))
        store.attach(_record("agent-b", owner="key_b"))
        assert [t.agent_id for t in store.list_workflow("key_a", "wf-1")] == ["agent-a"]
        assert [t.agent_id for t in store.list_workflow("key_b", "wf-1")] == ["agent-b"]
        assert store.list_workflow("key_c", "wf-1") == []

    @pytest.mark.parametrize("store_kind", ["memory", "file"])
    def test_list_sorted_by_created_at_then_agent(self, tmp_path, store_kind) -> None:
        store = _stores(tmp_path)[store_kind == "file"]
        store.attach(_record("agent-2", task_id="t-2", created_at="2026-01-02T00:00:00+00:00"))
        store.attach(_record("agent-1", task_id="t-1", created_at="2026-01-01T00:00:00+00:00"))
        store.attach(_record("agent-3", task_id="t-3", created_at="2026-01-01T00:00:00+00:00"))
        assert [t.agent_id for t in store.list_workflow("key_a", "wf-1")] == [
            "agent-1",
            "agent-3",
            "agent-2",
        ]

    def test_for_agent_unknown_is_none(self, tmp_path) -> None:
        for store in _stores(tmp_path):
            assert store.for_agent("ghost") is None
            assert store.list_workflow("key_a", "wf-ghost") == []


class TestDurability:
    def test_file_store_reopen_survives_restart(self, tmp_path) -> None:
        root = tmp_path / "wf"
        store1 = FileWorkflowStore(root)
        store1.attach(_record("agent-1", task_id="t-1", role="worker"))
        store1.attach(_record("agent-2", task_id="t-2", role="reviewer", parent_task_id="t-1"))
        # Simulated control-plane restart: a new store over the same dir.
        store2 = FileWorkflowStore(root)
        tasks = store2.list_workflow("key_a", "wf-1")
        assert [t.agent_id for t in tasks] == ["agent-1", "agent-2"]
        assert tasks[1].parent_task_id == "t-1"
        assert store2.for_agent("agent-2").task_id == "t-2"

    def test_corrupt_index_falls_back_to_agent_scan(self, tmp_path) -> None:
        root = tmp_path / "wf"
        store = FileWorkflowStore(root)
        store.attach(_record("agent-1"))
        store.attach(_record("agent-2", task_id="t-2"))
        index_path = root / "index" / "key_a" / "wf-1.json"
        index_path.write_text("{not json", encoding="utf-8")
        tasks = store.list_workflow("key_a", "wf-1")
        assert sorted(t.agent_id for t in tasks) == ["agent-1", "agent-2"]

    def test_corrupt_index_shape_falls_back_to_scan(self, tmp_path) -> None:
        store = InMemoryWorkflowStore()
        store.attach(_record("agent-1"))
        store._indexes[("key_a", "wf-1")] = {"tasks": "not-a-dict"}
        assert [t.agent_id for t in store.list_workflow("key_a", "wf-1")] == ["agent-1"]

    def test_corrupt_agent_record_is_skipped(self, tmp_path) -> None:
        root = tmp_path / "wf"
        store = FileWorkflowStore(root)
        store.attach(_record("agent-1"))
        bad = root / "agents" / "agent-bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert store.for_agent("agent-bad") is None
        # A corrupt agent record cannot poison a workflow listing.
        assert [t.agent_id for t in store.list_workflow("key_a", "wf-1")] == ["agent-1"]

    def test_record_from_dict_rejects_bad_shapes(self) -> None:
        for bad in (
            "nope",
            {"owner": "k"},
            {"owner": "k", "workflow_id": "w", "task_id": "t", "role": "r"},
            {"owner": "k", "workflow_id": "w", "task_id": "t", "role": "r", "agent_id": ""},
            {
                "owner": "k",
                "workflow_id": "w",
                "task_id": "t",
                "role": "r",
                "agent_id": "a",
                "parent_task_id": 7,
            },
        ):
            with pytest.raises(ValueError):
                record_from_dict(bad)


class TestLeakHygiene:
    def test_records_never_carry_credential_fields(self, tmp_path) -> None:
        store = FileWorkflowStore(tmp_path / "wf")
        store.attach(_record("agent-1", task_id="t-1", role="worker"))
        text = json.dumps([p.read_text() for p in (tmp_path / "wf").rglob("*.json")]).lower()
        for marker in ("token", "secret", "password", "credential", "auth"):
            assert marker not in text
