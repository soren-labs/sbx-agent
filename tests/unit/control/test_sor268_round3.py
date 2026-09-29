"""SOR-268 repair round 3 invariants (SOR-271 re-gate FAIL on v21).

1. ``GET /v2/sessions`` still scaled ~linearly with history @333 rows —
   ``finished`` rows still paid the live settle (``plane.get`` + a
   per-agent ledger list each). Now every stored-terminal row projects
   from the record + workspace map alone: the only live input that can
   still flip ``finished`` is the delivery outcome, and the ws map
   already carries it.
2. List reads spiked ~4.5s under 20x SSE fanout — the same per-row
   remote-op burst, bounded by (1).
3. Bulk cancel ACK p95 1522ms: a worker finishing inside the budget —
   the common case when cancelling terminal/queued rows — then rebuilt
   a full live ``_view`` on the request path (settle + ws + ledger +
   live extras). Now every mutation done path answers from one point
   read + the durable record — the worker already converged it.
4. Create ACK regressed 832→~1050ms: the keyed dedup read paid two
   serial RTTs (idem point row, then owner scan) and an unkeyed create's
   ``put`` paid a cold owner-doc read. Now ``find_by_idempotency``
   issues both reads concurrently and stages the owner doc for ``put``,
   and every create — keyed or not — fires ``prefetch_owner``.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from control.api_v1.state import V1State
from control.api_v2 import routes as v2_routes
from control.api_v2.schemas import CreateSessionRequest
from control.ports import ApiKey
from control.run_store import InMemoryRunStore
from control.tasks import (
    InMemoryTaskStore,
    ModalDictTaskStore,
    TaskRecord,
    _task_summary,
    record_to_dict,
)
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
    """A ``Request`` stand-in: only ``.app.state.v2_ack_budget_s`` is read."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(v2_ack_budget_s=budget)))


class TestTerminalProjection:
    """``finished`` rows project delivery truth from the ws map — no
    live settle, no ledger read — and every other terminal status is the
    stored verdict verbatim."""

    def _record(self, status: str, **kw: Any) -> TaskRecord:
        rec = _task("sess_p", status=status, agent_id="agent-p", **kw)
        return rec

    def test_finished_pending_delivery_projects_delivering(self) -> None:
        rec = self._record("finished")
        rec.resolved = {"git": {"push": True}}
        status, reason = v2_routes._terminal_projection(rec, None)
        assert (status, reason) == ("delivering", "delivery_pending")

    def test_finished_failed_delivery_projects_delivery_failed(self) -> None:
        rec = self._record("finished")
        rec.resolved = {"git": {"push": True}}
        status, reason = v2_routes._terminal_projection(
            rec, {"git": {"push": True}, "publish_error": "boom"}
        )
        assert (status, reason) == ("delivery_failed", "delivery_failed")

    def test_finished_delivered_projects_finished(self) -> None:
        rec = self._record("finished")
        rec.resolved = {"git": {"push": True}}
        status, reason = v2_routes._terminal_projection(
            rec, {"git": {"push": True}, "pushed_head_sha": "abc"}
        )
        assert (status, reason) == ("finished", "run_finished")

    def test_finished_no_delivery_requirement_projects_finished(self) -> None:
        status, reason = v2_routes._terminal_projection(self._record("finished"), None)
        assert (status, reason) == ("finished", "run_finished")

    def test_other_terminals_project_stored(self) -> None:
        for status in ("cancelled", "error", "expired", "delivery_failed"):
            got = v2_routes._terminal_projection(self._record(status), None)
            assert got == (status, "stored")


class TestListTerminalRowsCostZeroOps:
    """The list page's remote cost tracks LIVE rows only — a page of
    history pays exactly the task owner doc + ws index gets."""

    def test_finished_history_pays_no_settle(self) -> None:
        task_store = _SpyTaskStore()
        for i in range(30):
            task_store.put(
                _task(
                    f"sess_f{i:03d}",
                    status="finished",
                    agent_id=f"agent-{i}",
                    created=f"2026-09-29T00:{i:02d}:00+00:00",
                )
            )
        task_store.put(_task("sess_live", status="queued", agent_id=None))
        ws_store, fake = _ws_store()
        ws_store._dict.data["__workspaces__"] = {}  # index exists — no migration scan
        plane = _FakePlane(ws_store)
        fake.gets = 0
        fake.items_calls = 0
        task_store.puts.clear()

        out = v2_routes.list_sessions(
            key=_key(),
            task_store=task_store,
            run_states=InMemoryRunStore(),
            plane=plane,
            limit=100,
            offset=0,
        )
        assert out["total"] == 31
        # ONE remote op on the page: the workspace index get. The 30
        # finished rows would each have paid a live settle before round 3.
        assert fake.gets == 1
        assert fake.items_calls == 0
        assert plane.get_calls == []
        assert task_store.puts == []
        assert out["sessions"][0]["status"] == "finished"

    def test_delivery_flip_shows_and_writes_back_once(self) -> None:
        """A ``finished`` row whose ws delivery failed projects failed —
        and converges the stored row in one bounded write."""
        task_store = _SpyTaskStore()
        rec = _task("sess_f", status="finished", agent_id="agent-x")
        rec.resolved = {"git": {"push": True}}
        task_store.put(rec)
        ws_store, _fake = _ws_store()
        # Seed the ws row directly on the fake dict (index path read).
        ws_store._dict.data["__workspaces__"] = {
            "agent-x": {"agent_id": "agent-x", "git": {"push": True}, "publish_error": "boom"}
        }
        plane = _FakePlane(ws_store)
        task_store.puts.clear()
        out = v2_routes.list_sessions(
            key=_key(),
            task_store=task_store,
            run_states=InMemoryRunStore(),
            plane=plane,
            limit=100,
            offset=0,
        )
        row = next(s for s in out["sessions"] if s["id"] == "sess_f")
        assert row["status"] == "failed"
        assert task_store.puts == ["sess_f"]  # the single convergence write
        assert task_store.get("sess_f").status == "delivery_failed"


class _GetCountingTaskStore(_SpyTaskStore):
    def __init__(self) -> None:
        super().__init__()
        self.gets: list[str] = []

    def get(self, task_id: str) -> TaskRecord | None:
        self.gets.append(task_id)
        return super().get(task_id)


class TestMutationAckSkipsLiveView:
    """Done-path ACKs answer from the converged record — one point read,
    never a ``_view`` rebuild (settle + ws + ledger + live extras)."""

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

    def test_cancel_done_path(self, monkeypatch: Any) -> None:
        task_store = _GetCountingTaskStore()
        task_store.put(_task("sess_c", status="running", agent_id="agent-c"))
        ws_store, ws_fake = _ws_store()
        plane = _FakePlane(ws_store)

        def _cancel(task_id: str, **kw: Any) -> dict[str, Any]:
            rec = task_store.get(task_id)
            rec.status = "cancelled"
            task_store.put(rec)
            return {"task": {}}

        monkeypatch.setattr(v2_routes._tasks, "cancel_task", _cancel)
        out = v2_routes.cancel_session("sess_c", _req(budget=0.5), **self._deps(task_store, plane))
        assert out["session"]["status"] == "cancelled"
        # The ACK built from one point read — a live _view would have
        # called plane.get (live extras) and the ws store.
        assert plane.get_calls == []
        assert ws_fake.gets == 0
        assert task_store.gets[-1] == "sess_c"

    def test_retry_done_path(self, monkeypatch: Any) -> None:
        task_store = _GetCountingTaskStore()
        task_store.put(_task("sess_r", status="finished", agent_id="agent-r"))
        ws_store, ws_fake = _ws_store()
        plane = _FakePlane(ws_store)

        def _retry(task_id: str, mode: str, **kw: Any) -> dict[str, Any]:
            rec = task_store.get(task_id)
            rec.status = "queued"
            task_store.put(rec)
            return {"record": rec, "run": None}

        monkeypatch.setattr(v2_routes._tasks, "retry_task", _retry)
        out = v2_routes.retry_session("sess_r", _req(budget=0.5), **self._deps(task_store, plane))
        assert out["session"]["status"] == "queued"
        assert out["run"] is None
        assert plane.get_calls == []
        assert ws_fake.gets == 0

    def test_timeout_path_still_optimistic(self, monkeypatch: Any) -> None:
        task_store = _GetCountingTaskStore()
        task_store.put(_task("sess_t", status="running", agent_id="agent-t"))
        plane = _FakePlane()

        def _cancel(task_id: str, **kw: Any) -> dict[str, Any]:
            time.sleep(0.3)
            rec = task_store.get(task_id)
            rec.status = "cancelled"
            task_store.put(rec)
            return {"task": {}}

        monkeypatch.setattr(v2_routes._tasks, "cancel_task", _cancel)
        out = v2_routes.cancel_session("sess_t", _req(budget=0.05), **self._deps(task_store, plane))
        # Budget beat: optimistic cancelled ACK, worker converged behind it.
        assert out["session"]["status"] == "cancelled"
        time.sleep(0.4)
        assert task_store.get("sess_t").status == "cancelled"


class _ModalStore(ModalDictTaskStore):
    def __init__(self) -> None:
        super().__init__("test-tasks")
        self._dict = _BatchDict()


def _owner_doc_for(records: list[TaskRecord]) -> dict[str, Any]:
    """The owner index row ``put`` would have written."""
    return {
        "ids": [r.id for r in records],
        "records": {r.id: _task_summary(r) for r in records},
    }


class TestFindByIdempotency:
    """One wall round trip for the dedup read (idem + owner concurrent),
    with the owner doc staged for the imminent ``put``."""

    def test_miss_scans_owner_doc_and_stages_for_put(self) -> None:
        store = _ModalStore()
        fake = store._dict
        prior = _task("sess_a", owner="key_1", status="finished")
        prior.idempotency = {"key": "v2:session:K1", "fingerprint": "fp"}
        fake.data[f"task/{prior.id}"] = record_to_dict(prior)
        fake.data["owner/key_1"] = _owner_doc_for([prior])

        got = store.find_by_idempotency("key_1", "v2:session:K1")
        assert got is not None and got.id == "sess_a"
        # Reads: idem point get + owner get (concurrent) + one fresh task row.
        assert fake.gets == 3
        # The owner doc is staged — the put skips the RMW read; the only
        # get left is the one-time ``__owners__`` index for an unseen
        # owner (deduped in-memory for steady-state writes).
        gets_before = fake.gets
        store.put(_task("sess_new", owner="key_1"))
        assert fake.gets == gets_before + 1
        # A later put without a fresh prefetch pays the RMW owner read
        # again — the staged row is consumed once.
        store.put(_task("sess_new2", owner="key_1"))
        assert fake.gets == gets_before + 2

    def test_point_hit_returns_fresh_row(self) -> None:
        store = _ModalStore()
        fake = store._dict
        prior = _task("sess_a", owner="key_1", status="finished")
        prior.idempotency = {"key": "v2:session:K1", "fingerprint": "fp"}
        fake.data[f"task/{prior.id}"] = record_to_dict(prior)
        fake.data[ModalDictTaskStore._idem_key("key_1", "v2:session:K1")] = prior.id
        got = store.find_by_idempotency("key_1", "v2:session:K1")
        assert got is not None and got.id == "sess_a"

    def test_preindex_row_matched_via_pooled_get(self) -> None:
        store = _ModalStore()
        fake = store._dict
        prior = _task("sess_a", owner="key_1", status="finished")
        prior.idempotency = {"key": "v2:session:K1", "fingerprint": "fp"}
        fake.data[f"task/{prior.id}"] = record_to_dict(prior)
        # Pre-index shape: ids only, no summary records.
        fake.data["owner/key_1"] = {"ids": ["sess_a"], "records": {}}
        got = store.find_by_idempotency("key_1", "v2:session:K1")
        assert got is not None and got.id == "sess_a"

    def test_no_match_returns_none(self) -> None:
        store = _ModalStore()
        store._dict.data["owner/key_1"] = {"ids": [], "records": {}}
        assert store.find_by_idempotency("key_1", "v2:session:nope") is None


class _PrefetchTaskStore(InMemoryTaskStore):
    def __init__(self) -> None:
        super().__init__()
        self.prefetches: list[str] = []

    def prefetch_owner(self, owner: str) -> None:
        self.prefetches.append(owner)


class TestCreatePrefetchesEveryCreate:
    """``put``'s owner-doc read is issued before the budget on EVERY
    create — not only the keyed path (round 3 regression fix)."""

    def _create(self, store: Any, monkeypatch: Any, idem: str | None) -> dict[str, Any]:
        def _slow_once(*a: Any, **kw: Any) -> dict[str, Any]:
            time.sleep(0.3)
            return {"task": {}, "run": None}

        monkeypatch.setattr(v2_routes._tasks, "_create_task_once", _slow_once)
        return v2_routes.create_session(
            CreateSessionRequest(prompt="hi"),
            _req(budget=0.05),
            key=_key(),
            plane=_FakePlane(),
            registry=SimpleNamespace(),
            scheduler=SimpleNamespace(),
            v1=V1State(),
            run_states=InMemoryRunStore(),
            reporter=None,
            workflows=SimpleNamespace(),
            resources_registry=None,
            capabilities=SimpleNamespace(),
            task_store=store,
            resolver=SimpleNamespace(),
            idempotency_key=idem,
        )

    def test_unkeyed_create_fires_prefetch(self, monkeypatch: Any) -> None:
        store = _PrefetchTaskStore()
        out = self._create(store, monkeypatch, idem=None)
        assert store.prefetches == ["key_1"]
        assert out["session"]["status"] == "queued"

    def test_keyed_create_fires_prefetch_once(self, monkeypatch: Any) -> None:
        store = _PrefetchTaskStore()
        out = self._create(store, monkeypatch, idem="K1")
        assert store.prefetches == ["key_1"]  # not double-fired
        assert out["session"]["status"] == "queued"
