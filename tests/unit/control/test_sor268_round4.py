"""SOR-268 repair round 4 invariants (SOR-271 re-gate FAIL on v22).

The residual failures were all ambient-op contention on the shared
remote-op channel, not per-request op counts:

1. List-under-20x-SSE p95 ~4.5s unchanged across round 3 — the hub's
   per-tick ``_aggregate_status`` (``plane.get`` + ledger list +
   throttled reconcile probe ≈ ~4-9 ops/s per live hub) keeps the
   serialized channel busy; a list call's 1-2 remote ops then queue
   behind seconds of ambient work. Now stored-terminal records project
   without live reads (``_status_aggregate``), and both the store's
   owner page and the rendered route page memoize ~1s — a repeat list
   inside the window pays ZERO remote ops.
2. Bulk-cancel ACK p95 1637ms — the budgeted-worker wait still paid the
   ``_HEAVY_POOL`` queue depth (~10-15 ops per ``cancel_task`` chain).
   Now a ``cancel/<id>`` intent marker is persisted in ONE lock-free
   ``Dict.update`` and the ACK fires immediately; the full cancel chain
   converges on the pool behind it. The marker deliberately does NOT
   flip ``record.status`` — the worker's ``_settle_task`` must still see
   the row live or its already-cancelled short-circuit would skip the
   real kill/drain.
3. Cold list 13.9s — the v2 stores' lazy ``Dict.from_name`` connects and
   first index reads all paid inside the first request; the startup
   warmer now touches them.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

from control.api_v1.state import V1State
from control.api_v2 import routes as v2_routes
from control.ports import ApiKey
from control.run_store import InMemoryRunStore
from control.tasks import FileTaskStore, InMemoryTaskStore, ModalDictTaskStore
from tests.unit.control.test_sor268_async_perf import _BatchDict
from tests.unit.control.test_sor271_round2 import (
    _FakePlane,
    _SpyTaskStore,
    _task,
    _ws_store,
)


def _key() -> ApiKey:
    return ApiKey(id="key_1", key_hash="h", label="t")


def _req(budget: float = 0.5) -> Any:
    """A ``Request`` stand-in: ``.app.state`` carries the ack budget and
    the list memo dict."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(v2_ack_budget_s=budget)))


class _ModalTasks(ModalDictTaskStore):
    def __init__(self) -> None:
        super().__init__("test-tasks")
        self._dict = _BatchDict()


class TestOwnerPageMemo:
    """``list(owner)`` memoizes the owner doc ~1s; ``put``/``delete``
    write-through keep it coherent; ``list_fresh`` always re-reads."""

    def _seeded(self) -> tuple[_ModalTasks, _BatchDict]:
        store = _ModalTasks()
        store.put(_task("sess_a", status="finished", agent_id="agent-a"))
        store.put(_task("sess_b", status="cancelled", agent_id="agent-b"))
        return store, store._dict

    def _seeded_cold(self) -> tuple[_ModalTasks, _BatchDict]:
        """Seeded WITHOUT the write-through memo — the owner doc exists
        remotely but this process has not read it."""
        store, fake = self._seeded()
        store._owner_list_cache.clear()
        store._get_cache.clear()
        return store, fake

    def test_repeat_list_is_one_owner_doc_get(self) -> None:
        store, fake = self._seeded_cold()
        fake.gets = 0
        first = store.list("key_1")
        after_first = fake.gets
        store.list("key_1")
        store.list("key_1")
        assert len(first) == 2
        assert after_first == 1  # the owner doc
        assert fake.gets == 1  # memo served the repeats

    def test_own_put_is_already_memo_warm(self) -> None:
        store, fake = self._seeded()  # put() write-throughs the memo
        fake.gets = 0
        rows = store.list("key_1")
        assert len(rows) == 2
        assert fake.gets == 0

    def test_put_write_through_updates_memo(self) -> None:
        store, fake = self._seeded()
        store.list("key_1")
        store.put(_task("sess_c", status="queued", agent_id="agent-c"))
        fake.gets = 0
        rows = store.list("key_1")
        assert {r.id for r in rows} == {"sess_a", "sess_b", "sess_c"}
        assert fake.gets == 0  # write-through — no re-read needed

    def test_delete_write_through_updates_memo(self) -> None:
        store, fake = self._seeded()
        store.list("key_1")
        store.delete("sess_a")
        fake.gets = 0
        rows = store.list("key_1")
        assert {r.id for r in rows} == {"sess_b"}
        assert fake.gets == 0

    def test_list_fresh_bypasses_the_memo(self) -> None:
        store, fake = self._seeded()
        store.list("key_1")
        fake.gets = 0
        rows = store.list_fresh("key_1")
        assert len(rows) == 2
        assert fake.gets == 1  # mutation-path reads never run on memo

    def test_memo_expires(self) -> None:
        store, fake = self._seeded()
        store.list("key_1")
        hit = store._owner_list_cache["key_1"]
        store._owner_list_cache["key_1"] = (time.monotonic() - 10.0, hit[1], hit[2])
        fake.gets = 0
        store.list("key_1")
        assert fake.gets == 1


class TestWorkspaceListMemo:
    """``list_records`` memoizes the ``__workspaces__`` index ~1s."""

    def test_repeat_list_is_one_get(self) -> None:
        from tests.unit.control.test_sor271_round2 import _ws_record

        store, fake = _ws_store()
        store.put(_ws_record("a1"))
        store._list_cache = None  # simulate a cold process
        fake.gets = 0
        dict(store.list_records())
        dict(store.list_records())
        assert fake.gets == 1
        assert fake.items_calls == 0

    def test_list_records_fresh_bypasses(self) -> None:
        from tests.unit.control.test_sor271_round2 import _ws_record

        store, fake = _ws_store()
        store.put(_ws_record("a1"))
        dict(store.list_records())
        fake.gets = 0
        dict(store.list_records_fresh())
        assert fake.gets == 1

    def test_own_put_is_memo_warm(self) -> None:
        from tests.unit.control.test_sor271_round2 import _ws_record

        store, fake = _ws_store()
        store.put(_ws_record("a1"))  # write-throughs the memo
        fake.gets = 0
        assert dict(store.list_records())["a1"]["repo"] == "org/repo"
        assert fake.gets == 0


class TestCancelIntentMarker:
    """``cancel/<id>`` — one lock-free write, invisible to consumers,
    never a premature ``status == 'cancelled'`` on the record."""

    def test_mark_is_one_update_zero_gets(self) -> None:
        store = _ModalTasks()
        fake = store._dict
        store.put(_task("sess_x", status="running", agent_id="agent-x"))
        fake.gets = 0
        fake.updates = 0
        assert store.mark_cancel_pending("sess_x") is True
        assert fake.updates == 1
        assert fake.gets == 0  # lock-free — no owner-doc RMW
        mark = store.cancel_mark("sess_x")
        assert mark == {"task_id": "sess_x", "at": mark["at"], "applied": False}
        # The record is untouched — the worker's settle must still see it
        # live, or the already-cancelled short-circuit skips the kill.
        assert store.get("sess_x").status == "running"

    def test_mark_applied_flips_the_marker(self) -> None:
        store = _ModalTasks()
        store.mark_cancel_pending("sess_x")
        store.mark_cancel_applied("sess_x")
        assert store.cancel_mark("sess_x")["applied"] is True

    def test_marker_never_appears_in_listing(self) -> None:
        store = _ModalTasks()
        store.put(_task("sess_x", status="running", agent_id="agent-x"))
        store.mark_cancel_pending("sess_x")
        assert [r.id for r in store.list("key_1")] == ["sess_x"]
        assert store.list_fresh("key_1")[0].id == "sess_x"

    def test_inmemory_and_file_marks(self, tmp_path: Any) -> None:
        for store in (InMemoryTaskStore(), FileTaskStore(tmp_path / "tasks")):
            assert store.cancel_mark("sess_x") is None
            assert store.mark_cancel_pending("sess_x") is True
            assert store.cancel_mark("sess_x")["applied"] is False
            store.mark_cancel_applied("sess_x")
            assert store.cancel_mark("sess_x")["applied"] is True
            # FileTaskStore.list must not surface the cancel-*.json marker.
            if isinstance(store, FileTaskStore):
                store.put(_task("sess_real"))
                assert [r.id for r in store.list()] == ["sess_real"]


class _NoMarkTaskStore(_SpyTaskStore):
    """A store without the marker lane — exercises the budget fallback."""

    mark_cancel_pending = None  # type: ignore[assignment]
    mark_cancel_applied = None  # type: ignore[assignment]
    cancel_mark = None  # type: ignore[assignment]


class TestCancelAcksOnIntent:
    """The ACK follows the durable intent write — the ``cancel_task``
    chain converges on the ops pool behind it, never inside the wait."""

    def _deps(self, task_store: Any, plane: Any) -> dict[str, Any]:
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

    def test_marked_path_ack_does_not_wait_for_worker(self, monkeypatch: Any) -> None:
        task_store = InMemoryTaskStore()
        task_store.put(_task("sess_c_mk", status="running", agent_id="agent-c"))
        plane = _FakePlane()
        calls: list[str] = []

        def _slow_cancel(task_id: str, **kw: Any) -> dict[str, Any]:
            # Detached pool workers resolve ``_tasks.cancel_task`` at call
            # time — a straggler from a neighbouring test may land here;
            # only act on this test's own id.
            if task_id != "sess_c_mk":
                return {"task": {}}
            calls.append(task_id)
            time.sleep(0.35)
            rec = task_store.get(task_id)
            rec.status = "cancelled"
            task_store.put(rec)
            return {"task": {}}

        monkeypatch.setattr(v2_routes._tasks, "cancel_task", _slow_cancel)
        started = time.monotonic()
        out = v2_routes.cancel_session(
            "sess_c_mk", _req(budget=0.05), **self._deps(task_store, plane)
        )
        elapsed = time.monotonic() - started
        # The ACK did not wait out the 0.35s worker — it returns on the
        # point read alone; intent + converge land on the ops pool.
        assert elapsed < 0.30
        assert out["session"]["status"] == "cancelled"
        # The full chain — marker, cancel, applied — converges behind it.
        deadline = time.monotonic() + 5.0
        while task_store.get("sess_c_mk").status != "cancelled" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls == ["sess_c_mk"]
        assert task_store.get("sess_c_mk").status == "cancelled"
        deadline = time.monotonic() + 5.0
        mark = task_store.cancel_mark("sess_c_mk")
        while (mark is None or not mark["applied"]) and time.monotonic() < deadline:
            time.sleep(0.01)
            mark = task_store.cancel_mark("sess_c_mk")
        assert mark is not None and mark["applied"] is True

    def test_terminal_replay_never_starts_a_worker(self, monkeypatch: Any) -> None:
        task_store = InMemoryTaskStore()
        task_store.put(_task("sess_t", status="finished", agent_id="agent-t"))
        calls: list[str] = []

        def _cancel(task_id: str, **kw: Any) -> dict[str, Any]:
            if task_id != "sess_t":  # stray pool-worker replay from a neighbour test
                return {"task": {}}
            calls.append(task_id)
            return {"task": {}}

        monkeypatch.setattr(v2_routes._tasks, "cancel_task", _cancel)
        out = v2_routes.cancel_session(
            "sess_t", _req(budget=0.5), **self._deps(task_store, _FakePlane())
        )
        assert out["session"]["status"] == "finished"
        assert calls == []  # idempotent replay — nothing to converge
        assert task_store.cancel_mark("sess_t") is None

    def test_bulk_cancel_acks_bounded_under_contended_channel(self, monkeypatch: Any) -> None:
        """20 parallel cancels: each ACK pays only get + one marker
        update — no wait on the worker chain behind the ops-pool queue."""
        task_store = InMemoryTaskStore()
        for i in range(20):
            task_store.put(_task(f"sess_b{i:02d}", status="running", agent_id=f"agent-b{i}"))

        def _slow_cancel(task_id: str, **kw: Any) -> dict[str, Any]:
            if not task_id.startswith("sess_b"):
                return {"task": {}}  # stray pool-worker replay
            time.sleep(0.25)
            rec = task_store.get(task_id)
            rec.status = "cancelled"
            task_store.put(rec)
            return {"task": {}}

        monkeypatch.setattr(v2_routes._tasks, "cancel_task", _slow_cancel)
        plane = _FakePlane()
        lat: list[float] = []

        def _one(tid: str) -> None:
            t0 = time.monotonic()
            out = v2_routes.cancel_session(tid, _req(budget=0.05), **self._deps(task_store, plane))
            lat.append(time.monotonic() - t0)
            assert out["session"]["status"] == "cancelled"

        threads = [threading.Thread(target=_one, args=(f"sess_b{i:02d}",)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(lat) == 20
        # Every ACK is the point read alone — p95 well under the 1s
        # budget while the 20 worker chains queue on the 6-slot ops pool.
        assert sorted(lat)[18] < 1.0
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not all(
            task_store.cancel_mark(f"sess_b{i:02d}") is not None for i in range(20)
        ):
            time.sleep(0.02)
        for i in range(20):
            assert task_store.cancel_mark(f"sess_b{i:02d}") is not None

    def test_unmarked_store_falls_back_to_budgeted_path(self, monkeypatch: Any) -> None:
        task_store = _NoMarkTaskStore()
        task_store.put(_task("sess_n", status="running", agent_id="agent-n"))

        def _cancel(task_id: str, **kw: Any) -> dict[str, Any]:
            if task_id != "sess_n":
                return {"task": {}}  # stray pool-worker replay
            rec = task_store.get(task_id)
            rec.status = "cancelled"
            task_store.put(rec)
            return {"task": {}}

        monkeypatch.setattr(v2_routes._tasks, "cancel_task", _cancel)
        out = v2_routes.cancel_session(
            "sess_n", _req(budget=0.5), **self._deps(task_store, _FakePlane())
        )
        assert out["session"]["status"] == "cancelled"


class TestStatusAggregateTerminalShortCircuit:
    """The hub tick must not pay live reads for a stored-terminal
    record — at ~2 ticks/s/hub that ambient rate is what starved reads
    under 20x fanout."""

    def test_terminal_projection_pays_no_plane_or_ledger(self) -> None:
        plane = _FakePlane()
        rec = _task("sess_t", status="finished", agent_id="agent-t")
        status, reason = v2_routes._status_aggregate(rec, None, InMemoryRunStore(), plane)
        assert (status, reason) == ("finished", "run_finished")
        assert plane.get_calls == []

    def test_live_record_still_aggregates(self) -> None:
        plane = _FakePlane()
        rec = _task("sess_l", status="running", agent_id="agent-l")
        status, _reason = v2_routes._status_aggregate(rec, None, InMemoryRunStore(), plane)
        assert plane.get_calls == ["agent-l"]  # live reads preserved
        assert status in {"running", "queued"}


class TestRouteListMemo:
    """A repeat ``GET /v2/sessions`` inside the TTL is served with ZERO
    remote ops — under SSE fanout the page is the contended read."""

    def _list(self, task_store: Any, ws_store: Any, req: Any) -> dict[str, Any]:
        return v2_routes.list_sessions(
            req,
            key=_key(),
            task_store=task_store,
            run_states=InMemoryRunStore(),
            plane=_FakePlane(ws_store),
            limit=100,
            offset=0,
        )

    def test_repeat_list_is_memo_served(self) -> None:
        task_store = _SpyTaskStore()
        task_store.put(_task("sess_a", status="finished", agent_id="agent-a"))
        ws_store, fake = _ws_store()
        ws_store._dict.data["__workspaces__"] = {}
        req = _req()
        self._list(task_store, ws_store, req)
        fake.gets = 0
        task_store.puts.clear()
        out = self._list(task_store, ws_store, req)
        assert out["total"] == 1
        assert fake.gets == 0  # memo — not even the ws index get

    def test_mutation_drop_forces_refetch(self, monkeypatch: Any) -> None:
        task_store = InMemoryTaskStore()
        task_store.put(_task("sess_a", status="finished", agent_id="agent-a"))
        ws_store, fake = _ws_store()
        ws_store._dict.data["__workspaces__"] = {}
        req = _req()
        assert self._list(task_store, ws_store, req)["total"] == 1
        # A store-level write the route didn't make is hidden while the
        # rendered-page memo stands.
        task_store.put(_task("sess_b", status="finished", agent_id="agent-b"))
        assert self._list(task_store, ws_store, req)["total"] == 1
        # A mutation on the route drops the owner's page — the very next
        # list recomputes (the ws index read itself may be memo-served at
        # the store layer, which is the layered design).
        v2_routes.cancel_session(
            "sess_a",
            req,
            key=_key(),
            plane=_FakePlane(ws_store),
            v1=V1State(),
            run_states=InMemoryRunStore(),
            workflows=SimpleNamespace(),
            scheduler=SimpleNamespace(),
            reporter=None,
            task_store=task_store,
        )
        assert self._list(task_store, ws_store, req)["total"] == 2
