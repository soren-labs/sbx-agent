"""Focused regressions for the direct SOR-268 production-latency repair."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from control.api_v1.state import V1State
from control.api_v2 import routes as v2_routes
from control.ports import ApiKey
from control.run_store import InMemoryRunStore
from control.tasks import ModalDictTaskStore
from tests.unit.control.test_sor268_async_perf import _BatchDict
from tests.unit.control.test_sor268_round3 import _GetCountingTaskStore, _req
from tests.unit.control.test_sor271_round2 import _FakePlane, _task, _ws_record, _ws_store


def _key() -> ApiKey:
    return ApiKey(id="key_1", key_hash="h", label="t")


def _deps(task_store: Any, plane: Any) -> dict[str, Any]:
    return {
        "key": _key(),
        "plane": plane,
        "v1": V1State(),
        "run_states": InMemoryRunStore(),
        "workflows": SimpleNamespace(),
        "scheduler": SimpleNamespace(),
        "reporter": None,
        "task_store": task_store,
    }


def _modal_task_store() -> tuple[ModalDictTaskStore, _BatchDict]:
    store = ModalDictTaskStore("test-tasks")
    fake = _BatchDict()
    store._dict = fake
    return store, fake


def test_task_owner_list_memo_eliminates_second_remote_get() -> None:
    store, fake = _modal_task_store()
    for i in range(4):
        store.put(_task(f"sess_{i}", status="finished", agent_id=f"agent-{i}"))
    fake.gets = 0

    first = store.list("key_1")
    after_first = fake.gets
    second = store.list("key_1")

    assert len(first) == len(second) == 4
    assert after_first == 1
    assert fake.gets == after_first


def test_workspace_index_memo_eliminates_second_remote_get() -> None:
    store, fake = _ws_store()
    store.put(_ws_record("agent-a"))
    store.put(_ws_record("agent-b"))
    fake.gets = 0

    first = dict(store.list_records())
    after_first = fake.gets
    second = dict(store.list_records())

    assert sorted(first) == sorted(second) == ["agent-a", "agent-b"]
    assert after_first == 1
    assert fake.gets == after_first


def test_modal_cancel_marker_is_one_write_and_zero_reads() -> None:
    store, fake = _modal_task_store()
    fake.gets = 0
    before_updates = fake.updates

    assert store.mark_cancel_pending("sess_x") is True

    assert fake.gets == 0
    assert fake.updates == before_updates + 1
    assert fake.data["cancel/sess_x"]["applied"] is False


def test_cancel_persists_marker_before_background_convergence(monkeypatch: Any) -> None:
    events: list[str] = []

    class _IntentStore(_GetCountingTaskStore):
        def mark_cancel_pending(self, task_id: str) -> bool:
            events.append("mark")
            return True

        def mark_cancel_applied(self, task_id: str) -> None:
            events.append("applied")

    store = _IntentStore()
    store.put(_task("sess_c", status="running", agent_id="agent-c"))
    plane = _FakePlane()

    def _cancel(task_id: str, **_: Any) -> dict[str, Any]:
        events.append("cancel")
        rec = store.get(task_id)
        assert rec is not None
        rec.status = "cancelled"
        store.put(rec)
        return {"task": {}}

    monkeypatch.setattr(v2_routes, "_CANCEL_CONVERGE_DELAY_S", 0.01)
    monkeypatch.setattr(v2_routes._tasks, "cancel_task", _cancel)

    out = v2_routes.cancel_session("sess_c", _req(budget=0.5), **_deps(store, plane))
    assert out["session"]["status"] == "cancelled"
    assert events[0] == "mark"

    deadline = time.monotonic() + 1.0
    while "applied" not in events and time.monotonic() < deadline:
        time.sleep(0.01)
    assert events[:3] == ["mark", "cancel", "applied"]


def test_cancel_does_not_trust_expired_terminal_cache(monkeypatch: Any) -> None:
    store, fake = _modal_task_store()
    stale = _task("sess_retry", status="finished", agent_id="agent-r")
    store.put(stale)

    # Simulate another control-plane instance retrying the session: the
    # authoritative Dict row is running while this process still holds an
    # expired terminal cache entry.
    live = _task("sess_retry", status="running", agent_id="agent-r")
    fake.data[store._task_key("sess_retry")] = {
        **fake.data[store._task_key("sess_retry")],
        "status": "running",
        "updated_at": live.updated_at,
    }
    key = store._task_key("sess_retry")
    with store._lock:
        ts, raw = store._get_cache[key]
        store._get_cache[key] = (ts - 60.0, raw)

    called = []

    def _cancel(task_id: str, **_: Any) -> dict[str, Any]:
        called.append(task_id)
        return {"task": {}}

    monkeypatch.setattr(v2_routes, "_CANCEL_CONVERGE_DELAY_S", 0.0)
    monkeypatch.setattr(v2_routes._tasks, "cancel_task", _cancel)

    out = v2_routes.cancel_session(
        "sess_retry",
        _req(budget=0.5),
        **_deps(store, _FakePlane()),
    )
    assert out["session"]["status"] == "cancelled"

    deadline = time.monotonic() + 1.0
    while not called and time.monotonic() < deadline:
        time.sleep(0.01)
    assert called == ["sess_retry"]


def test_terminal_status_aggregate_never_touches_live_plane() -> None:
    rec = _task("sess_done", status="finished", agent_id="agent-done")

    class _ExplodingPlane:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"terminal aggregate touched plane.{name}")

    status, reason = v2_routes._status_aggregate(
        rec,
        None,
        InMemoryRunStore(),
        _ExplodingPlane(),
    )
    assert (status, reason) == ("finished", "run_finished")


def test_rendered_list_memo_skips_repeated_projection_work() -> None:
    store = _GetCountingTaskStore()
    store.put(_task("sess_done", status="finished", agent_id="agent-done"))
    ws_store, fake = _ws_store()
    ws_store._dict.data["__workspaces__"] = {}
    plane = _FakePlane(ws_store)

    v2_routes._list_memo_drop(store, "key_1")
    first = v2_routes.list_sessions(
        key=_key(),
        task_store=store,
        run_states=InMemoryRunStore(),
        plane=plane,
        limit=100,
        offset=0,
    )
    gets_after_first = fake.gets
    second = v2_routes.list_sessions(
        key=_key(),
        task_store=store,
        run_states=InMemoryRunStore(),
        plane=plane,
        limit=100,
        offset=0,
    )

    assert first == second
    assert fake.gets == gets_after_first
