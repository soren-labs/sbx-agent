"""SOR-268 async-ACK + indexed read-model invariants.

Pins the remote-call budgets the SOR-260 gate found blown: batched
``Dict.update`` writes, one-call owner lists, per-record read-through
caches with fresh-read bypass on mutation paths, bounded async teardown
on ``stop``/``close``, the read-path reconcile cooldown, and the shared
SSE fanout (one tail + one poller per session, N client queues).
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from control import tasks as taskmod
from control.backend import SandboxHandle, SandboxPoll, SandboxSpec
from control.run_store import ModalDictRunStore, RunLedger, RunRecord
from control.service import ControlPlane
from control.store import ModalDictStore, SessionRecord
from control.tasks import ModalDictTaskStore, TaskRecord
from control.workspace import ModalDictWorkspaceStore
from tests.unit.control.test_dict_listing_indexes import _FakeDict


class _BatchDict(_FakeDict):
    """``modal.Dict`` 1.5.5 surface: ``update`` is ONE remote call
    (``DictUpdateRequest``) writing many keys; the counter proves the
    batch path never degrades into per-key puts."""

    def __init__(self) -> None:
        super().__init__()
        self.updates = 0

    def update(self, mapping):
        self.updates += 1
        self.data.update(mapping)

    def contains(self, key):
        return key in self.data

    def pop(self, key, default=None):
        return self.data.pop(key, default)


def _now() -> datetime:
    return datetime(2026, 9, 29, tzinfo=UTC)


def _session(agent_id: str, owner: str = "key-1") -> SessionRecord:
    now = _now()
    return SessionRecord(
        id=agent_id,
        title="t",
        status="idle",
        created_at=now,
        updated_at=now,
        model="m",
        turns=0,
        usage=None,
        messages=[],
        owner=owner,
        sandbox_tags={"provider": "codex", "account_id": "acct-1"},
    )


def _task(task_id: str, owner: str = "key_1") -> TaskRecord:
    return TaskRecord(
        id=task_id,
        owner=owner,
        status="queued",
        request={"prompt": {"text": "x"}},
        resolved={"execution": {"provider": "codex"}},
        agent_id="agent-1",
        run_id=None,
        created_at="2026-09-29T00:00:00+00:00",
        updated_at="2026-09-29T00:00:00+00:00",
        response={"task": {"id": task_id}},
        idempotency={"key_id": owner, "key": f"idem-{task_id}", "fingerprint": "fp"},
    )


def _task_store() -> tuple[ModalDictTaskStore, _BatchDict]:
    store = ModalDictTaskStore("test-tasks")
    fake = _BatchDict()
    store._dict = fake
    return store, fake


class TestTaskStoreIndexedWrites:
    def test_put_is_one_read_one_batch_write(self) -> None:
        store, fake = _task_store()
        store.put(_task("sess_a"))
        # First-seen owner: __owners__ get + owner-doc get, then ONE
        # Dict.update carrying record + index rows (never per-key puts).
        assert fake.gets == 2
        assert fake.puts == 0
        assert fake.updates == 1
        assert fake.data["task/sess_a"]["id"] == "sess_a"

    def test_second_put_stays_batched(self) -> None:
        store, fake = _task_store()
        store.put(_task("sess_a"))
        fake.gets = 0
        store.put(_task("sess_b"))
        assert fake.updates == 2
        assert fake.puts == 0
        assert fake.gets == 1  # owner-doc only — __owners__ is warm
        assert sorted(fake.data["owner/key_1"]["ids"]) == ["sess_a", "sess_b"]

    def test_list_is_one_owner_doc_get(self) -> None:
        store, fake = _task_store()
        for i in range(8):
            store.put(_task(f"sess_{i}"))
        fake.gets = 0
        records = store.list("key_1")
        assert sorted(r.id for r in records) == [f"sess_{i}" for i in range(8)]
        assert fake.gets == 1  # the owner doc — no N+1 fanout

    def test_summary_drops_response_payload(self) -> None:
        store, fake = _task_store()
        store.put(_task("sess_a"))
        summary = fake.data["owner/key_1"]["records"]["sess_a"]
        assert summary["response"] is None  # payload dropped, key kept
        # The authoritative record keeps it.
        assert store.get("sess_a").response == {"task": {"id": "sess_a"}}

    def test_find_by_agent_is_point_indexed(self) -> None:
        store, fake = _task_store()
        store.put(_task("sess_a"))
        fake.gets = 0
        got = store.find_by_agent("agent-1")
        assert got is not None and got.id == "sess_a"
        assert fake.gets <= 2  # agent index row + record (no owner scan)

    def test_find_by_idempotency_is_point_indexed(self) -> None:
        store, fake = _task_store()
        rec = _task("sess_a")
        store.put(rec)
        fake.gets = 0
        got = store.find_by_idempotency("key_1", "idem-sess_a")
        assert got is not None and got.id == "sess_a"
        # Round 3: the owner-doc read is issued concurrently with the
        # point lookup — one overlapped get (staged for ``put``), still
        # no owner scan: idem row + owner doc + record = 3.
        assert fake.gets <= 3
        assert fake.items_calls == 0

    def test_missing_index_rows_backfill(self) -> None:
        store, fake = _task_store()
        # Pre-index record: bare task key only.
        raw = taskmod.record_to_dict(_task("sess_old"))
        fake.data["task/sess_old"] = raw
        fake.data["owner/key_1"] = {"ids": ["sess_old"], "records": {}}
        records = store.list("key_1")
        assert [r.id for r in records] == ["sess_old"]

    def test_get_is_cached_then_fresh(self) -> None:
        store, fake = _task_store()
        store.put(_task("sess_a"))
        fake.gets = 0
        store.get("sess_a")
        assert fake.gets == 0  # write-through cache hit
        store.get_fresh("sess_a")
        assert fake.gets == 1  # mutation-path reads never run on cache

    def test_list_settle_writeback_preserves_pinned_response(self) -> None:
        """SOR-268 review: a ``list`` record carries no ``response`` — a
        settle write-back during a status transition must merge the
        stored row's pinned create reply, not erase it."""
        store, fake = _task_store()
        store.put(_task("sess_a"))
        listed = store.list("key_1")[0]
        assert listed.response is None  # summary materialization
        # The settle write-back: status transition + put of the listed row.
        listed.status = "running"
        listed.transitions.append({"status": "running", "reason": "run", "at": "t"})
        store.put(listed)
        fresh = store.get_fresh("sess_a")
        assert fresh is not None
        assert fresh.response == {"task": {"id": "sess_a"}}
        assert fresh.status == "running"  # the transition itself landed

    def test_response_merge_only_for_summary_records(self) -> None:
        """A full-record put stays authoritative — merge never revives a
        response the caller deliberately cleared, and an unflagged write
        keeps the ~2-remote-op budget (no extra stored-row read)."""
        store, fake = _task_store()
        rec = _task("sess_a")
        store.put(rec)
        rec.response = None  # explicit clear through a full record
        store.put(rec)
        fake.gets = 0
        rec.status = "running"
        store.put(rec)  # unflagged path — no merge read
        assert fake.gets == 1  # owner-doc read only
        assert store.get_fresh("sess_a").response is None

    def test_summary_flag_survives_only_until_first_put(self) -> None:
        store, fake = _task_store()
        store.put(_task("sess_a"))
        listed = store.list("key_1")[0]
        store.put(listed)  # merges + clears the flag
        fake.gets = 0
        listed.status = "closed"
        store.put(listed)  # second put pays no merge read
        assert fake.gets == 1
        assert store.get_fresh("sess_a").response == {"task": {"id": "sess_a"}}

    def test_agent_less_summary_re_reads_authoritative_row(self) -> None:
        """SOR-271: a pre-bind record has no agent to live-aggregate
        against, so a stale index summary wedges the list at ``queued``
        while ``task/<id>`` already went terminal. Agent-less summaries
        are distrusted and point-read instead."""
        store, fake = _task_store()
        rec = _task("sess_a")
        rec.agent_id = None
        rec.status = "error"
        rec.transitions.append(
            {
                "status": "error",
                "reason": "dispatch_failed",
                "at": "t",
                "detail": {"code": "concurrency_limit", "message": "cap", "retryable": True},
            }
        )
        store.put(rec)
        # Simulate the lost index write: the owner-doc summary still says
        # queued while the authoritative row is terminal.
        fake.data["owner/key_1"]["records"]["sess_a"]["status"] = "queued"
        fake.gets = 0
        listed = store.list("key_1")
        assert [r.status for r in listed] == ["error"]
        # owner doc + one authoritative point read + the backfill's own
        # doc re-read inside the merge lock — the fetched row's terminal
        # status is healable, so the summary is rewritten (SOR-268 r5).
        assert fake.gets == 3

        # A bound record's summary still materializes without a point read.
        store.put(_task("sess_b"))
        fake.data["owner/key_1"]["records"]["sess_b"]["status"] = "queued"
        fake.gets = 0
        listed = store.list("key_1")
        assert {r.id: r.status for r in listed} == {"sess_a": "error", "sess_b": "queued"}
        # owner doc only — sess_a's healed summary is terminal-trusted and
        # sess_b is agent-bound (SOR-268 r5 self-heal).
        assert fake.gets == 2  # owner doc + bounded unbound-terminal validation


class TestRunStoreReadCache:
    def _store(self) -> tuple[ModalDictRunStore, _BatchDict]:
        store = ModalDictRunStore("test-runs")
        fake = _BatchDict()
        store._dict = fake
        store._index = _BatchDict()
        return store, fake

    def _run(self, agent: str, n: int) -> RunRecord:
        return RunRecord(
            agent_id=agent,
            n=n,
            status="RUNNING",
            created_at="2026-09-29T00:00:00+00:00",
            updated_at="2026-09-29T00:00:00+00:00",
        )

    def test_warm_get_hits_cache(self) -> None:
        store, fake = self._store()
        store.put(self._run("a", 1))
        fake.gets = 0
        store.get("a", 1)
        assert fake.gets == 0
        store.get_fresh("a", 1)
        assert fake.gets == 1

    def test_ledger_mutations_use_fresh_reads(self) -> None:
        store, fake = self._store()
        ledger = RunLedger(store)
        ledger.begin(agent_id="a", n=1, status="QUEUED")
        store.get("a", 1)  # warm the cache
        fake.gets = 0
        ledger.mark_running("a", 1)
        assert fake.gets >= 1  # the transition read bypassed the cache

    def test_list_fresh_bypasses_record_cache(self) -> None:
        store, fake = self._store()
        for n in (1, 2):
            store.put(self._run("a", n))
        store.list("a")  # warm
        fake.gets = 0
        ledger = RunLedger(store)
        out = ledger.list_fresh("a")
        assert [r.n for r in out] == [1, 2]
        assert fake.gets >= 2


class TestSessionStoreGetCache:
    def test_warm_get_and_put_budget(self) -> None:
        store, main, index = (
            ModalDictStore("test-sessions"),
            _BatchDict(),
            _BatchDict(),
        )
        store._dict = main
        store._index = index
        store.put(_session("ag-1"))
        assert main.updates == 0  # index doc lives on the index dict
        # Warm put: warm-id index bump + record put — two remote calls.
        main.puts = 0
        store.put(_session("ag-1"))
        assert main.puts == 1  # record put only; the index row was skipped
        main.gets = 0
        store.get("ag-1")
        assert main.gets == 0  # write-through cache
        store.get_fresh("ag-1")
        assert main.gets == 1


class TestWorkspaceStoreGetCache:
    def test_warm_get_cached_fresh_bypasses(self) -> None:
        from control.workspace import WorkspaceRecord

        store = ModalDictWorkspaceStore("test-workspaces")
        fake = _BatchDict()
        store._dict = fake
        rec = WorkspaceRecord(
            agent_id="ag-1",
            repo="o/r",
            base_ref="main",
            base_sha="a" * 40,
            created_at="2026-09-29T00:00:00+00:00",
            updated_at="2026-09-29T00:00:00+00:00",
        )
        store.put(rec)
        fake.gets = 0
        store.get("ag-1")
        assert fake.gets == 0
        store.get_fresh("ag-1")
        assert fake.gets == 1


class _BlockingBackend:
    """Sandbox backend whose remote calls block on events — stands in for
    multi-second Modal ops; the tests prove the caller never waits on
    them."""

    def __init__(self) -> None:
        self.handle = SandboxHandle(id="h-1", root=Path("/tmp/sbx-blocking"), tags={})
        self.terminated = threading.Event()
        self.block_terminate = threading.Event()
        self.exec_calls: list[list[str]] = []
        self.polls = 0
        self.block_exec = threading.Event()

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        return self.handle

    def exec(self, handle: SandboxHandle, argv: list[str], env=None, timeout=None):
        self.exec_calls.append(list(argv))

        class _P:
            stdout = iter(())

            def wait(self):
                return 0

            def kill(self):
                return None

        # Only the runner stop-hook blocks (the remote tail); provisioning
        # and file writes stay instant so the suite doesn't stall.
        if "stop" in argv:
            self.block_exec.wait(timeout=10)
        return _P()

    def terminate(self, handle: SandboxHandle) -> None:
        self.block_terminate.wait(timeout=10)
        self.terminated.set()

    def poll(self, handle: SandboxHandle) -> SandboxPoll:
        self.polls += 1
        return SandboxPoll(alive=False, active_processes=0)

    def list(self, tags=None) -> list[SandboxHandle]:
        return [self.handle]


def _plane_with_blocking_backend() -> tuple[ControlPlane, _BlockingBackend, Any]:
    from control.store import InMemoryStore

    backend = _BlockingBackend()
    store = InMemoryStore()
    plane = ControlPlane(backend, store, ["runner"], clock=_now)
    return plane, backend, store


class TestAsyncTeardown:
    def _attach(self, plane: ControlPlane, backend: _BlockingBackend, sid: str) -> None:
        rec = plane.store.get(sid)
        assert rec is not None
        rec.sandbox_id = backend.handle.id
        rec.sandbox_root = str(backend.handle.root)
        plane.store.put(rec)

    def test_close_returns_before_remote_teardown(self) -> None:
        plane, backend, _store = _plane_with_blocking_backend()
        sid = plane.create_session(owner="o", model="m", title="t")
        self._attach(plane, backend, sid)
        started = time.monotonic()
        rec = plane.close(sid)
        elapsed = time.monotonic() - started
        assert rec.status == "closed"  # durable intent landed
        assert elapsed < 5  # never serialized on the blocked terminate
        assert not backend.terminated.is_set()  # still converging
        backend.block_terminate.set()
        assert backend.terminated.wait(timeout=5)

    def test_stop_returns_before_remote_tail(self) -> None:
        plane, backend, _store = _plane_with_blocking_backend()
        sid = plane.create_session(owner="o", model="m", title="t")
        self._attach(plane, backend, sid)
        started = time.monotonic()
        final = plane.stop(sid)
        elapsed = time.monotonic() - started
        assert final == "idle"
        assert elapsed < 5
        backend.block_exec.set()  # release the stop-hook

    def test_ledger_cancel_lands_before_ack_wait_ends(self) -> None:
        from control.run_store import InMemoryRunStore

        plane, backend, _store = _plane_with_blocking_backend()
        plane.run_ledger = RunLedger(InMemoryRunStore())
        sid = plane.create_session(owner="o", model="m", title="t")
        self._attach(plane, backend, sid)
        rec = plane.store.get(sid)
        assert rec is not None
        rec.status = "running"
        rec.current_turn_n = 1
        rec.current_turn_id = "turn-1"
        plane.store.put(rec)
        plane.run_ledger.begin(agent_id=sid, n=1, status="RUNNING")
        final = plane.stop(sid)
        assert final == "idle"
        # The durable cancel lands under the lock, before any remote tail.
        record = plane.run_ledger.get(sid, 1)
        assert record is not None and record.status == "CANCELLED"


class TestReconcileCooldown:
    def test_maybe_reconcile_throttles_remote_probes(self, monkeypatch) -> None:
        import control.service as service

        monkeypatch.setattr(service, "_RECONCILE_COOLDOWN_S", 60.0)
        plane, backend, _store = _plane_with_blocking_backend()
        sid = plane.create_session(owner="o", model="m", title="t")
        rec = plane.store.get(sid)
        assert rec is not None
        rec.status = "running"
        rec.current_turn_n = 1
        rec.current_turn_id = "turn-1"
        rec.sandbox_id = backend.handle.id
        rec.sandbox_root = str(backend.handle.root)
        plane.store.put(rec)
        polls = backend.polls
        plane.maybe_reconcile_turn(sid)
        assert backend.polls == polls + 1  # first probe goes remote
        plane.maybe_reconcile_turn(sid)
        plane.maybe_reconcile_turn(sid)
        assert backend.polls == polls + 1  # inside cooldown: no repeats


class _QIter:
    """stdout iterator fed by a test queue — the tail never EOFs until the
    test enqueues the sentinel, so live/eof timing is deterministic."""

    def __init__(self, q: queue.Queue) -> None:
        self.q = q

    def __iter__(self):
        return self

    def __next__(self):
        item = self.q.get(timeout=10)
        if item is StopIteration:
            raise StopIteration
        return item


class TestEventsHubFanout:
    """One tail + one poller per session; N clients share them."""

    def _hub(self, *, procs: list, feeds: list[queue.Queue]):
        from control.api_v2 import events_hub

        class _Proc:
            def __init__(self, q):
                self.stdout = _QIter(q)

            def kill(self):
                return None

        def _start_tail(handle):
            feed: queue.Queue = queue.Queue()
            feeds.append(feed)
            proc = _Proc(feed)
            procs.append(proc)
            return proc

        frame = "event: session.status\ndata: {}\n\n"

        hub = events_hub.SessionEventsHub(
            "sess_x",
            record_probe=lambda: None,
            is_terminal_record=lambda r: False,
            status_bits=lambda: ("running", "run", frame),
            live_handle=lambda: (object(), type("P", (), {"alive": True})()),
            agent_terminal=lambda: False,
            start_tail=_start_tail,
            replay_lines=lambda: [],
            replay_entries=lambda: [],
        )
        return hub

    def test_two_subscribers_share_one_tail(self) -> None:
        from control.api_v2 import events_hub as hub_mod

        procs: list = []
        feeds: list[queue.Queue] = []
        hub = self._hub(procs=procs, feeds=feeds)
        deadline = time.monotonic() + 5
        while not feeds and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(procs) == 1  # one exec for the whole session
        sub_a, _open_a, _backlog_a = hub.subscribe(1)
        sub_b, _open_b, _backlog_b = hub.subscribe(1)
        feeds[0].put('{"type": "sbx.turn_started", "n": 1}\n')
        for sub in (sub_a, sub_b):
            got = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    item = sub.q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is hub_mod._CLOSE:
                    break
                if item.startswith("id:"):
                    got = item
                    break
            assert got is not None
        hub.unsubscribe(sub_a)
        hub.unsubscribe(sub_b)
        hub._dead.set()
        assert len(procs) == 1  # still one tail after both clients drained

    def test_eof_broadcasts_close(self) -> None:
        from control.api_v2 import events_hub as hub_mod

        procs: list = []
        feeds: list[queue.Queue] = []
        hub = self._hub(procs=procs, feeds=feeds)
        deadline = time.monotonic() + 5
        while not feeds and time.monotonic() < deadline:
            time.sleep(0.01)
        sub, _opening, _backlog = hub.subscribe(1)
        feeds[0].put(StopIteration)
        got_close = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                item = sub.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is hub_mod._CLOSE:
                got_close = True
                break
        hub.unsubscribe(sub)
        hub._dead.set()
        assert got_close  # tail EOF closed the stream for this client

    @staticmethod
    def _frame_ids(sub: Any, timeout: float = 5.0) -> list[int]:
        """Drain a subscriber queue; return the ``id:`` numbers of event
        frames received (status frames and the close sentinel skipped)."""
        from control.api_v2 import events_hub as hub_mod

        ids: list[int] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                item = sub.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is hub_mod._CLOSE:
                break
            if item.startswith("id:"):
                ids.append(int(item.split("\n", 1)[0][3:].strip()))
        return ids

    def test_retailed_transcript_keeps_line_numbers(self) -> None:
        """SOR-268 review: after a tail EOF the hub re-reads events.jsonl
        from line 1 — ids must stay the file line number, so a
        ``Last-Event-ID`` resume gets only genuinely new lines, never the
        transcript a second time under shifted ids."""
        procs: list = []
        feeds: list[queue.Queue] = []
        hub = self._hub(procs=procs, feeds=feeds)
        deadline = time.monotonic() + 5
        while not feeds and time.monotonic() < deadline:
            time.sleep(0.01)
        sub1, _o, _b = hub.subscribe(1)
        for n in (1, 2, 3):
            feeds[0].put(f'{{"type": "sbx.turn_started", "n": {n}}}\n')
        assert self._frame_ids(sub1) == [1, 2, 3]
        feeds[0].put(StopIteration)  # exec channel drop → EOF → close
        assert self._frame_ids(sub1) == []

        # Hub re-tails once the handle polls alive again.
        deadline = time.monotonic() + 5
        while len(feeds) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(feeds) == 2

        resumed, _o2, _b2 = hub.subscribe(4)  # Last-Event-ID: 3
        fresh, _o3, _b3 = hub.subscribe(1)
        for n in (1, 2, 3):  # the tail re-reads the whole file
            feeds[1].put(f'{{"type": "sbx.turn_started", "n": {n}}}\n')
        feeds[1].put('{"type": "sbx.turn_started", "n": 4}\n')
        deadline = time.monotonic() + 5
        while len(hub._frames) < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        with hub._lock:
            assert [lineno for lineno, _f in hub._frames] == [1, 2, 3, 4]
        assert self._frame_ids(resumed) == [4]  # dedup: no shifted re-replay
        assert self._frame_ids(fresh) == [1, 2, 3, 4]
        hub.unsubscribe(sub1)
        hub.unsubscribe(resumed)
        hub.unsubscribe(fresh)
        hub._dead.set()

    def test_replay_resolution_restarts_numbering(self) -> None:
        """Live frames followed by replay (terminal/unreachable) must not
        continue the live counter — replay ids are the transcript's own."""
        from control.api_v2 import events_hub

        hub = events_hub.SessionEventsHub(
            "sess_y",
            record_probe=lambda: None,
            is_terminal_record=lambda r: False,
            status_bits=lambda: ("closed", "done", "event: s\ndata: {}\n\n"),
            live_handle=lambda: (None, None),
            agent_terminal=lambda: False,  # hub stays waiting; resolve is manual
            start_tail=lambda h: None,
            replay_lines=lambda: [
                '{"type": "sbx.turn_started", "n": 1}\n',
                '{"type": "sbx.turn_started", "n": 2}\n',
            ],
            replay_entries=lambda: [],
        )
        # Ingest two live frames first (counter at 2), then resolve replay.
        hub._ingest_raw('{"type": "sbx.turn_started", "n": 1}\n')
        hub._ingest_raw('{"type": "sbx.turn_started", "n": 2}\n')
        hub._resolve_replay()
        with hub._lock:
            assert [lineno for lineno, _f in hub._frames] == [1, 2]
        hub._dead.set()


class _PlaneStub:
    """Bare ``plane`` surface ``_task_runs``/``_render_run`` touch."""

    def __init__(self, ledger: RunLedger) -> None:
        self.run_ledger = ledger

    def public(self, rec: Any) -> dict[str, Any]:
        return {
            "id": rec.id,
            "status": rec.status,
            "usage": {},
            "turns": 0,
            "created_at": "2026-09-29T00:00:00+00:00",
            "ended_at": "2026-09-29T00:00:01+00:00",
        }


class _RecStub:
    def __init__(self, agent_id: str) -> None:
        self.id = agent_id
        self.status = "closed"
        self.current_turn_n = None
        self.sandbox_tags = {}
        self.messages = []


class TestTaskRunsNoNPlusOne:
    """``_task_runs`` must render every run off ONE ``ledger.list`` — the
    per-run serial ``ledger.get`` is the B6/B7 N+1 the gate measured."""

    def test_runs_render_off_one_ledger_list(self) -> None:
        from control.api_v1 import tasks as _tasks
        from control.api_v1.lifecycle import InMemoryRunStates
        from control.api_v1.state import V1State

        store = ModalDictRunStore("sbx-run-test")
        fake = _BatchDict()
        store._dict = fake
        store._index = _BatchDict()
        ledger = RunLedger(store, clock=lambda: _now())
        agent_id = "agent-1"
        for n in (1, 2, 3):
            ledger.begin(agent_id=agent_id, n=n, status="RUNNING")
            ledger.finish(agent_id, n, status="FINISHED")
        fake.gets = 0
        rec = _RecStub(agent_id)
        v1 = V1State()
        runs = _tasks._task_runs(
            _PlaneStub(ledger),
            rec,
            v1,
            InMemoryRunStates(),
            scheduler=None,
            reporter=None,
        )
        assert [r["id"] for r in runs] == ["run-1", "run-2", "run-3"]
        assert fake.gets == 0  # no per-run ledger.get — the list rows fed every render
