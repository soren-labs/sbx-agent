"""SOR-271 re-gate (round 2) invariants.

Pins the three still-failing SOR-268 findings from the v20 gate plus the
routed zombie-agent finding:

1. Read starvation under SSE fanout: the V2 SSE handler ran remote Dict
   reads on the ASGI event loop (``async def`` connect path) and flooded
   the loop with per-frame yields on replay; the hub's cold
   ``opening_status`` let a 20-client reconnect herd pay 20 serial remote
   fan-ins. Now: sync handler on the worker pool, batched generator
   sends, one deduped cold compute.
2. ``GET /v2/sessions`` scaled ~linearly with history (per-row settle +
   per-row workspace fetch). Now: terminal rows skip the remote settle
   and the workspace map is ONE index-doc read on ``sbx-workspaces``
   (round 3 extends the skip to ``finished`` — the delivery outcome it
   could still flip on lives in the ws map).
3. Bulk cancel ACKs >1s p95: every request spawned an unbounded daemon
   thread, flooding the shared remote channel. Now: one bounded ops pool
   (``_HEAVY_POOL``) backs ``_run_with_budget``.
4. Zombie idle agents (up to 7h): one throwing ``backend.poll``/``put``/
   ``list_all`` aborted the whole 5-min sweep, and lost index-doc RMW
   writes made records invisible to ``list_all`` — both leak live-agent
   slots forever. Now: per-record guards + per-sweep + periodic
   ``rebuild_index`` self-heal.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from control import store as store_mod
from control.api_v2 import routes as v2_routes
from control.api_v2.events_hub import SessionEventsHub
from control.backend import LocalProcessBackend, SandboxSpec
from control.ports import ApiKey
from control.reaper import reap
from control.run_store import InMemoryRunStore
from control.store import InMemoryStore, ModalDictStore, SessionRecord, empty_usage
from control.tasks import InMemoryTaskStore, TaskRecord
from control.workspace import (
    FileWorkspaceStore,
    InMemoryWorkspaceStore,
    ModalDictWorkspaceStore,
    WorkspaceRecord,
)
from tests.unit.control.test_dict_listing_indexes import _FakeDict
from tests.unit.control.test_sor268_async_perf import _BatchDict


def _now() -> datetime:
    return datetime(2026, 9, 29, tzinfo=UTC)


def _ws_record(agent_id: str) -> WorkspaceRecord:
    return WorkspaceRecord(
        agent_id=agent_id,
        repo="org/repo",
        base_ref="main",
        base_sha="0" * 40,
        created_at=_now().isoformat(),
        updated_at=_now().isoformat(),
    )


def _ws_store() -> tuple[ModalDictWorkspaceStore, _BatchDict]:
    store = ModalDictWorkspaceStore("test-workspaces")
    fake = _BatchDict()
    store._dict = fake
    return store, fake


class TestWorkspaceIndex:
    """``sbx-workspaces`` index doc — the list path's one-get read."""

    def test_put_writes_row_and_index_in_one_batch(self) -> None:
        store, fake = _ws_store()
        store.put(_ws_record("a1"))
        assert fake.items_calls == 0
        assert fake.updates == 1  # row + __workspaces__ atomically
        assert fake.data["a1"]["agent_id"] == "a1"
        assert fake.data["__workspaces__"]["a1"]["repo"] == "org/repo"

    def test_list_records_is_one_index_get(self) -> None:
        store, fake = _ws_store()
        for i in range(5):
            store.put(_ws_record(f"a{i}"))
        fake.gets = 0
        records = dict(store.list_records())
        assert sorted(records) == [f"a{i}" for i in range(5)]
        assert fake.gets == 1
        assert fake.items_calls == 0

    def test_preindex_dict_falls_back_once_then_self_heals(self) -> None:
        store, fake = _ws_store()
        # Rows written before the index existed: no __workspaces__ doc.
        for i in range(3):
            fake.data[f"old{i}"] = {
                "agent_id": f"old{i}",
                "repo": "org/repo",
                "base_ref": "main",
                "base_sha": "0" * 40,
            }
        records = dict(store.list_records())
        assert sorted(records) == [f"old{i}" for i in range(3)]
        assert fake.items_calls == 1  # one migration scan…
        assert isinstance(fake.data.get("__workspaces__"), dict)  # …then healed
        fake.gets = 0
        dict(store.list_records())
        assert fake.gets == 1 and fake.items_calls == 1

    def test_delete_removes_index_entry_first(self) -> None:
        store, fake = _ws_store()
        store.put(_ws_record("a1"))
        order: list[tuple[str, Any]] = []

        orig_update = fake.update

        def _watch(mapping: dict[str, Any]) -> None:
            order.append(("update", dict(mapping)))
            orig_update(mapping)

        fake.update = _watch  # type: ignore[method-assign]
        store.delete("a1")
        assert "a1" not in fake.data["__workspaces__"]
        assert "a1" not in fake.data
        # The index write precedes the row pop — orphan, never ghost.
        assert order and "__workspaces__" in order[0][1]

    def test_inmemory_list_records(self) -> None:
        store = InMemoryWorkspaceStore()
        store.put(_ws_record("a1"))
        store.put(_ws_record("a2"))
        assert sorted(dict(store.list_records())) == ["a1", "a2"]

    def test_file_list_records(self, tmp_path: Any) -> None:
        store = FileWorkspaceStore(tmp_path / "ws")
        store.put(_ws_record("a1"))
        store.put(_ws_record("a2"))
        assert sorted(dict(store.list_records())) == ["a1", "a2"]


def _task(
    task_id: str,
    *,
    owner: str = "key_1",
    status: str = "queued",
    agent_id: str | None = None,
    created: str = "2026-09-29T00:00:00+00:00",
) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        owner=owner,
        status=status,
        request={"prompt": {"text": "x"}},
        resolved={"execution": {"provider": "codex"}},
        agent_id=agent_id,
        run_id=None,
        created_at=created,
        updated_at=created,
    )


class _SpyTaskStore(InMemoryTaskStore):
    def __init__(self) -> None:
        super().__init__()
        self.puts: list[str] = []

    def put(self, record: TaskRecord) -> None:
        self.puts.append(record.id)
        super().put(record)


class _FakePlane:
    """The slice of the control plane ``list_sessions`` touches."""

    def __init__(self, workspaces: Any = None) -> None:
        self.workspaces = workspaces
        self.get_calls: list[str] = []
        self.recs: dict[str, Any] = {}

    def get(self, agent_id: str) -> Any:
        self.get_calls.append(agent_id)
        return self.recs.get(agent_id)

    def public(self, rec: Any) -> dict[str, Any]:
        return rec.public() if hasattr(rec, "public") else {}


class TestListBoundedRead:
    """GET /v2/sessions: page cost tracks live rows, never history."""

    def _key(self) -> ApiKey:
        return ApiKey(id="key_1", key_hash="h", label="t")

    def test_terminal_rows_cost_zero_remote_ops(self) -> None:
        task_store = _SpyTaskStore()
        # 40 terminal-history rows + one unbound queued row (no agent →
        # _aggregate_status answers from the record alone).
        for i in range(40):
            task_store.put(
                _task(
                    f"sess_{i:04d}",
                    status="cancelled",
                    agent_id=f"agent-{i}",
                    created=f"2026-09-29T00:{i % 60:02d}:00+00:00",
                )
            )
        task_store.put(_task("sess_live", status="queued", agent_id=None))
        ws_store, fake = _ws_store()
        ws_store.put(_ws_record("agent-0"))
        plane = _FakePlane(ws_store)
        run_states = InMemoryRunStore()
        fake.gets = 0
        fake.items_calls = 0
        task_store.puts.clear()

        out = v2_routes.list_sessions(
            key=self._key(),
            task_store=task_store,
            run_states=run_states,
            plane=plane,
            limit=100,
            offset=0,
        )
        assert out["total"] == 41
        assert len(out["sessions"]) == 41
        # The whole page paid ONE remote op: the workspace index get.
        assert fake.gets == 1
        assert fake.items_calls == 0
        # Absorbing-terminal rows never settle: no plane.get, no write-back.
        assert plane.get_calls == []
        assert task_store.puts == []
        row = next(s for s in out["sessions"] if s["id"] == "sess_0000")
        assert row["status"] == "cancelled"

    def test_finished_rows_project_without_live_settle(self) -> None:
        """``finished`` is terminal-but-flippable only by the delivery
        outcome — which the ws map already carries — so the list page
        skips its live settle too (round 3: the residual ~linear term)."""
        task_store = _SpyTaskStore()
        task_store.put(_task("sess_f", status="finished", agent_id="agent-x"))
        ws_store, fake = _ws_store()
        plane = _FakePlane(ws_store)
        fake.gets = 0
        task_store.puts.clear()
        out = v2_routes.list_sessions(
            key=self._key(),
            task_store=task_store,
            run_states=InMemoryRunStore(),
            plane=plane,
            limit=100,
            offset=0,
        )
        assert plane.get_calls == []
        assert fake.gets == 1  # the index get only
        assert task_store.puts == []  # no flip → no write-back
        assert out["sessions"][0]["status"] == "finished"

    def test_error_rows_skip_the_settle(self) -> None:
        task_store = _SpyTaskStore()
        task_store.put(_task("sess_e", status="error", agent_id="agent-e"))
        ws_store, fake = _ws_store()
        plane = _FakePlane(ws_store)
        fake.gets = 0
        task_store.puts.clear()
        out = v2_routes.list_sessions(
            key=self._key(),
            task_store=task_store,
            run_states=InMemoryRunStore(),
            plane=plane,
            limit=100,
            offset=0,
        )
        assert plane.get_calls == []
        assert fake.gets == 1  # the index get only
        assert out["sessions"][0]["status"] == "failed"


class TestHeavyOpsPool:
    """``_run_with_budget`` shares ONE bounded pool — a 20× burst queues
    behind ``_V2_OPS_WORKERS`` workers instead of flooding the channel."""

    def test_burst_concurrency_is_bounded(self) -> None:
        inflight = 0
        max_seen = 0
        lock = threading.Lock()
        gate = threading.Event()

        def _work() -> None:
            nonlocal inflight, max_seen
            with lock:
                inflight += 1
                max_seen = max(max_seen, inflight)
            gate.wait(5)
            with lock:
                inflight -= 1

        burst = 2 * v2_routes._V2_OPS_WORKERS + 4
        threads = [
            threading.Thread(target=lambda: v2_routes._run_with_budget(_work, 0.05))
            for _ in range(burst)
        ]
        for t in threads:
            t.start()
        time.sleep(0.3)  # let the pool reach steady state
        gate.set()
        for t in threads:
            t.join(5)
        assert max_seen <= v2_routes._V2_OPS_WORKERS
        assert max_seen >= 1

    def test_timeout_still_converges_in_background(self) -> None:
        box_done = threading.Event()

        def _work() -> None:
            time.sleep(0.3)
            box_done.set()

        completed, _box = v2_routes._run_with_budget(_work, 0.01)
        assert completed is False  # ACK fired on budget
        assert box_done.wait(5)  # …and the submitted work still finished


class TestOpeningStatusDedupe:
    def _hub(self, calls: list) -> SessionEventsHub:
        frame = "event: session.status\ndata: {}\n\n"

        def _bits() -> tuple[str, str, str]:
            calls.append(1)
            time.sleep(0.15)  # stand in for the remote fan-in
            return "running", "run", frame

        return SessionEventsHub(
            "sess_x",
            record_probe=lambda: None,
            is_terminal_record=lambda r: False,
            status_bits=_bits,
            live_handle=lambda: (None, None),
            agent_terminal=lambda: True,  # never spawns a tail
            start_tail=lambda h: None,
            replay_lines=lambda: [],
            replay_entries=lambda: [],
        )

    def test_cold_herd_pays_one_compute(self) -> None:
        calls: list = []
        hub = self._hub(calls)
        # Park the ticker so the herd exercises the cold path alone.
        hub._dead.set()
        hub._thread.join(5)
        hub._status = None
        baseline = len(calls)

        results: list[str | None] = []
        threads = [
            threading.Thread(target=lambda: results.append(hub.opening_status())) for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert len(results) == 8 and all(r is not None for r in results)
        assert len(calls) == baseline + 1  # one compute for the whole herd

    def test_warm_opening_status_never_recomputes(self) -> None:
        calls: list = []
        hub = self._hub(calls)
        hub._dead.set()
        hub._thread.join(5)
        hub._status = ("running", "run", "frame")
        baseline = len(calls)
        assert hub.opening_status() == "frame"
        assert len(calls) == baseline


class TestSyncSseHandler:
    """The V2 SSE handler must be a ``def`` — remote reads on the ASGI
    loop were the >5s starvation spikes the gate measured."""

    def test_stream_session_events_is_not_a_coroutine(self) -> None:
        assert not asyncio.iscoroutinefunction(v2_routes.stream_session_events)

    def test_read_and_mutation_handlers_are_sync(self) -> None:
        assert not asyncio.iscoroutinefunction(v2_routes.list_sessions)
        assert not asyncio.iscoroutinefunction(v2_routes.cancel_session)


class TestReaperResilience:
    """Zombie-agent fix: one wedged record must never abort the sweep,
    and index drift must self-heal."""

    def _idle_record(self, session_id: str, handle: Any, minutes: int = 40) -> SessionRecord:
        t = _now() - timedelta(minutes=minutes)
        return SessionRecord(
            id=session_id,
            title="t",
            status="idle",
            created_at=t,
            updated_at=t,
            model="m",
            turns=0,
            usage=empty_usage(),
            messages=[],
            owner="sbx",
            sandbox_id=handle.id,
            sandbox_root=str(handle.root),
            sandbox_tags={"session_id": session_id, "owner": "sbx"},
            last_activity_at=t,
        )

    def test_one_bad_poll_does_not_starve_the_sweep(self) -> None:
        backend = LocalProcessBackend()
        h1 = backend.create(SandboxSpec(tags={"session_id": "s1", "owner": "sbx"}))
        h2 = backend.create(SandboxSpec(tags={"session_id": "s2", "owner": "sbx"}))
        store = InMemoryStore()
        store.put(self._idle_record("s1", h1))
        store.put(self._idle_record("s2", h2))

        class _FlakyPoll(LocalProcessBackend):
            def poll(self, handle):  # type: ignore[override]
                if handle.id == h1.id:
                    raise RuntimeError("modal wedged")
                return super().poll(handle)

        actions = reap(store, _FlakyPoll(), _now(), idle_timeout_s=300)
        kinds = {(a.kind, a.session_id) for a in actions}
        assert ("timed_out", "s2") in kinds
        assert ("reap_error", "s1") in kinds
        # s1 stays non-terminal — retried on the next sweep.
        assert store.get("s1").status == "idle"

    def test_put_failure_continues_the_sweep(self) -> None:
        backend = LocalProcessBackend()
        h1 = backend.create(SandboxSpec(tags={"session_id": "s1", "owner": "sbx"}))
        h2 = backend.create(SandboxSpec(tags={"session_id": "s2", "owner": "sbx"}))

        class _FlakyStore(InMemoryStore):
            fail = False

            def put(self, record):  # type: ignore[override]
                if self.fail and record.id == "s1":
                    raise RuntimeError("dict write wedged")
                super().put(record)

        store = _FlakyStore()
        store.put(self._idle_record("s1", h1))
        store.put(self._idle_record("s2", h2))
        store.fail = True
        actions = reap(store, backend, _now(), idle_timeout_s=300)
        kinds = {(a.kind, a.session_id) for a in actions}
        assert ("reap_error", "s1") in kinds
        assert ("timed_out", "s2") in kinds

    def test_list_all_failure_is_survivable(self) -> None:
        class _Broken(InMemoryStore):
            def list_all(self):  # type: ignore[override]
                raise RuntimeError("index dict unavailable")

        actions = reap(_Broken(), LocalProcessBackend(), _now(), idle_timeout_s=300)
        assert any(a.kind == "reap_error" for a in actions)

    def test_rebuild_index_runs_once_per_sweep(self) -> None:
        calls: list[int] = []

        class _HealingStore(InMemoryStore):
            def rebuild_index(self) -> int:
                calls.append(1)
                return 0

        reap(_HealingStore(), LocalProcessBackend(), _now(), idle_timeout_s=300)
        assert calls == [1]


class TestListAllIndexSelfHeal:
    """The sessions store rebuilds its id manifest on the background
    refresh cadence — a lost cross-container RMW entry heals instead of
    leaking the record (and its live-agent slot) forever."""

    def _store(self) -> tuple[ModalDictStore, _FakeDict, _BatchDict]:
        store = ModalDictStore("test-sessions")
        d = _FakeDict()
        idx = _BatchDict()
        store._dict = d
        store._index = idx
        return store, d, idx

    def _put(self, store: ModalDictStore, session_id: str) -> None:
        store.put(
            SessionRecord(
                id=session_id,
                title="t",
                status="idle",
                created_at=_now(),
                updated_at=_now(),
                model="m",
                turns=0,
                usage=empty_usage(),
                messages=[],
                owner="sbx",
            )
        )

    def test_refresh_rebuilds_the_manifest_past_the_interval(self) -> None:
        store, d, idx = self._store()
        self._put(store, "s1")
        store.list_all()  # warm: builds + caches
        # Simulate a cross-container RMW loss: row present, id dropped.
        ghost = SessionRecord(
            id="ghost",
            title="t",
            status="idle",
            created_at=_now(),
            updated_at=_now(),
            model="m",
            turns=0,
            usage=empty_usage(),
            messages=[],
            owner="sbx",
        )
        d.data["ghost"] = store_mod.record_to_dict(ghost)
        idx.data["agents"] = ["s1"]
        store._list_cache = None
        assert [r.id for r in store.list_all()] == ["s1"]
        store._last_rebuild_at = 0.0  # past the interval
        keys_before = d.keys_calls
        # ``_refresh_listing`` releases ``_refresh_lock`` itself — the
        # production caller is ``_kick_refresh`` (nonblocking acquire).
        assert store._refresh_lock.acquire(blocking=False)
        store._refresh_listing()
        assert d.keys_calls > keys_before
        store._list_cache = None
        assert "ghost" in [r.id for r in store.list_all()]

    def test_refresh_skips_rebuild_inside_the_interval(self) -> None:
        store, d, _idx = self._store()
        self._put(store, "s1")
        store.list_all()
        store._last_rebuild_at = time.monotonic()
        keys_before = d.keys_calls
        assert store._refresh_lock.acquire(blocking=False)
        store._refresh_listing()
        assert d.keys_calls == keys_before
