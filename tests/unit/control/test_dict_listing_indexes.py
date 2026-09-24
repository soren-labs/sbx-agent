"""SOR-199 listing indexes: Modal-Dict-backed stores must list via index
manifests + bounded point gets, never ``items()``/``keys()`` enumeration.

``modal.Dict`` enumeration is server-paged at ~one round-trip per key —
at production scale (195 agents / 11k+ artifact keys) that put
``GET /v1/agents``, ``/v1/agents/{id}/runs`` and the unfiltered
``/v1/artifacts`` listing over their latency budgets. The counting fake
bills one call per primitive RPC so op-count asserts hold for the real
backend too.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from control.artifacts import (
    ArtifactManifest,
    ModalDictArtifactStore,
    build_artifact,
    manifest_dumps,
)
from control.run_store import (
    ModalDictRunStore,
    RunLedger,
    RunRecord,
)
from control.run_store import (
    record_to_dict as run_record_to_dict,
)
from control.store import ModalDictStore, SessionRecord, record_to_dict
from control.workflow_store import ModalDictWorkflowStore, WorkflowTaskRecord


class _FakeDict:
    """``modal.Dict``-shaped fake: string keys, arbitrary values, call
    counters proving which RPC surface a method used."""

    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.gets = 0
        self.puts = 0
        self.items_calls = 0
        self.keys_calls = 0
        self.fail_get = False

    def get(self, key, default=None):
        self.gets += 1
        if self.fail_get:
            raise RuntimeError("dict unavailable")
        return self.data.get(key, default)

    def put(self, key, value):
        self.puts += 1
        self.data[key] = value

    def pop(self, key):
        return self.data.pop(key)

    def keys(self):
        self.keys_calls += 1
        return iter(list(self.data))

    def items(self):
        self.items_calls += 1
        return iter(list(self.data.items()))


def _session(agent_id: str, i: int = 0) -> SessionRecord:
    now = datetime(2026, 9, 20, tzinfo=UTC)
    return SessionRecord(
        id=agent_id,
        title=f"agent-{agent_id}",
        status="idle",
        created_at=now,
        updated_at=now,
        model="m",
        turns=1,
        usage=None,
        messages=[],
        owner="key-1",
        sandbox_tags={"provider": "codex", "account_id": "acct-1"},
    )


def _run(agent_id: str, n: int) -> RunRecord:
    return RunRecord(
        agent_id=agent_id,
        n=n,
        status="FINISHED",
        created_at="2026-09-20T00:00:00+00:00",
        updated_at="2026-09-20T00:01:00+00:00",
    )


class TestSessionListingIndex:
    def _store(self) -> tuple[ModalDictStore, _FakeDict, _FakeDict]:
        store = ModalDictStore("test-sessions")
        main, index = _FakeDict(), _FakeDict()
        store._dict = main  # tests never touch real modal
        store._index = index
        return store, main, index

    def test_list_all_uses_manifest_not_items(self) -> None:
        store, main, index = self._store()
        for i in range(5):
            store.put(_session(f"ag-{i}"))
        store.list_all()  # cold call absorbs the one-time migration rebuild
        main.items_calls = 0
        main.keys_calls = 0
        records = store.list_all()
        assert sorted(r.id for r in records) == [f"ag-{i}" for i in range(5)]
        assert main.items_calls == 0
        assert main.keys_calls == 0  # warm path: manifest get + point gets only

    def test_lazy_migration_rebuilds_once(self) -> None:
        store, main, index = self._store()
        # Pre-index data: records written directly, no index doc.
        for i in range(4):
            rec = _session(f"old-{i}")
            main.put(rec.id, record_to_dict(rec))
        records = store.list_all()
        assert sorted(r.id for r in records) == [f"old-{i}" for i in range(4)]
        assert index.data[ModalDictStore._IDX_BUILT] == b"1"
        keys_calls = main.keys_calls
        store.list_all()
        assert main.keys_calls == keys_calls  # warm path stays indexed

    def test_stale_id_skipped(self) -> None:
        store, main, index = self._store()
        store.put(_session("live"))
        index.put(ModalDictStore._IDX_IDS, ["ghost", "live"])
        index.put(ModalDictStore._IDX_BUILT, b"1")
        records = store.list_all()
        assert [r.id for r in records] == ["live"]

    def test_delete_removes_index_row(self) -> None:
        store, main, index = self._store()
        store.put(_session("a"))
        store.put(_session("b"))
        store.delete("a")
        assert index.data[ModalDictStore._IDX_IDS] == ["b"]
        assert [r.id for r in store.list_all()] == ["b"]

    def test_index_failure_falls_back_to_scan(self) -> None:
        store, main, index = self._store()
        rec = _session("x")
        main.put(rec.id, record_to_dict(rec))
        index.fail_get = True
        index.put(ModalDictStore._IDX_BUILT, b"1")  # marker read also fails
        records = store.list_all()
        assert [r.id for r in records] == ["x"]
        assert main.items_calls >= 1

    def test_put_index_failure_invalidates_marker(self) -> None:
        store, main, index = self._store()
        index.put(ModalDictStore._IDX_BUILT, b"1")
        index.fail_get = True
        store.put(_session("y"))  # index add fails; record still written
        assert ModalDictStore._IDX_BUILT not in index.data
        assert main.data["y"] is not None


class TestRunListingIndex:
    def _store(self) -> tuple[ModalDictRunStore, _FakeDict, _FakeDict]:
        store = ModalDictRunStore("test-runs")
        main, index = _FakeDict(), _FakeDict()
        store._dict = main
        store._index = index
        return store, main, index

    def test_list_uses_index_not_items(self) -> None:
        store, main, index = self._store()
        for agent in ("a", "b"):
            for n in (1, 2):
                store.put(_run(agent, n))
        main.items_calls = 0
        assert [r.n for r in store.list("a")] == [1, 2]
        assert [r.n for r in store.list("b")] == [1, 2]
        assert main.items_calls == 0

    def test_lazy_migration_rebuilds_per_agent_docs(self) -> None:
        store, main, index = self._store()
        # Simulate pre-index data written straight to the Dict.
        for n in (1, 2, 3):
            main.put(f"legacy/{n}", run_record_to_dict(_run("legacy", n)))
        runs = store.list("legacy")
        assert [r.n for r in runs] == [1, 2, 3]
        assert index.data[ModalDictRunStore._IDX_BUILT] == b"1"
        assert index.data[store._idx_key("legacy")] == [1, 2, 3]
        items_calls = main.items_calls
        store.list("legacy")
        assert main.items_calls == items_calls

    def test_stale_run_number_skipped(self) -> None:
        store, main, index = self._store()
        store.put(_run("a", 1))
        index.put(store._idx_key("a"), [1, 9])
        index.put(ModalDictRunStore._IDX_BUILT, b"1")
        assert [r.n for r in store.list("a")] == [1]

    def test_delete_removes_run_number(self) -> None:
        store, main, index = self._store()
        for n in (1, 2):
            store.put(_run("a", n))
        store.delete("a", 1)
        assert index.data[store._idx_key("a")] == [2]
        assert [r.n for r in store.list("a")] == [2]

    def test_rebuild_drops_empty_agents(self) -> None:
        store, main, index = self._store()
        store.put(_run("a", 1))
        index.put(store._idx_key("gone"), [4])
        store.rebuild_index()
        assert store._idx_key("gone") not in index.data
        assert index.data[store._idx_key("a")] == [1]


class TestWorkflowListingManifest:
    def _store(self) -> tuple[ModalDictWorkflowStore, _FakeDict]:
        store = ModalDictWorkflowStore("test-workflows")
        fake = _FakeDict()
        store._dict = fake
        return store, fake

    def _attach(self, store, agent_id: str, wf: str = "wf-1") -> None:
        store.attach(
            WorkflowTaskRecord(
                owner="key-1",
                workflow_id=wf,
                task_id=f"t-{agent_id}",
                role="worker",
                agent_id=agent_id,
            )
        )

    def test_all_bindings_uses_manifest_not_items(self) -> None:
        store, fake = self._store()
        for i in range(4):
            self._attach(store, f"ag-{i}")
        fake.items_calls = 0
        bindings = store.all_bindings()
        assert sorted(bindings) == [f"ag-{i}" for i in range(4)]
        assert fake.items_calls == 0

    def test_list_workflow_uses_manifest(self) -> None:
        store, fake = self._store()
        self._attach(store, "ag-1", "wf-a")
        self._attach(store, "ag-2", "wf-b")
        fake.items_calls = 0
        tasks = store.list_workflow("key-1", "wf-a")
        assert [t.agent_id for t in tasks] == ["ag-1"]
        assert fake.items_calls == 0

    def test_lazy_migration_rebuilds_manifest(self) -> None:
        store, fake = self._store()
        # Pre-manifest data: agent record keys written directly.
        fake.put(
            "wf-agent/old-1",
            {
                "owner": "key-1",
                "workflow_id": "wf-x",
                "task_id": "t",
                "role": "worker",
                "agent_id": "old-1",
                "created_at": "2026-09-20T00:00:00+00:00",
                "updated_at": "2026-09-20T00:00:00+00:00",
            },
        )
        bindings = store.all_bindings()
        assert sorted(bindings) == ["old-1"]
        assert fake.data[ModalDictWorkflowStore._AGENTS_MANIFEST] == ["old-1"]

    def test_manifest_failure_falls_back_to_scan(self) -> None:
        store, fake = self._store()
        self._attach(store, "ag-9")
        fake.fail_get = True
        bindings = store.all_bindings()
        assert "ag-9" in bindings  # items() fallback still lists it
        assert fake.items_calls >= 1


class TestArtifactGlobalIndex:
    def _store(self) -> tuple[ModalDictArtifactStore, _FakeDict]:
        store = ModalDictArtifactStore("test-artifacts")
        fake = _FakeDict()
        store._dict = fake
        return store, fake

    def _put(self, store, artifact_id: str, agent: str = "ag", i: int = 0) -> None:
        import tempfile
        from datetime import timedelta
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "a.txt").write_text("x\n", encoding="utf-8")
            base = datetime(2026, 9, 15, tzinfo=UTC) + timedelta(seconds=i)
            build_artifact(
                ws,
                store=store,
                agent_id=agent,
                artifact_id=artifact_id,
                clock=lambda: base,
            )

    def test_unfiltered_list_uses_global_index(self) -> None:
        store, fake = self._store()
        for i in range(6):
            self._put(store, f"art-{i}", agent=f"ag-{i % 2}", i=i)
        store.list()  # cold call absorbs the one-time migration rebuild
        fake.items_calls = 0
        fake.keys_calls = 0
        manifests = store.list()
        assert [m.artifact_id for m in manifests] == [f"art-{i}" for i in range(6)]
        assert fake.items_calls == 0
        assert fake.keys_calls == 0

    def test_unfiltered_page_keyset_walk(self) -> None:
        store, fake = self._store()
        for i in range(7):
            self._put(store, f"art-{i}", i=i)
        store.list_page(limit=1)  # warm the index
        fake.items_calls = 0
        fake.keys_calls = 0
        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            page = store.list_page(cursor=cursor, limit=3)
            seen += [m.artifact_id for m in page.artifacts]
            pages += 1
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
            assert pages < 10
        assert seen == [f"art-{i}" for i in range(7)]
        assert fake.items_calls == 0
        assert fake.keys_calls == 0

    def test_lazy_migration_builds_global_index(self) -> None:
        store, fake = self._store()
        # Pre-index data: manifests written without the global index.
        other = ModalDictArtifactStore("test-artifacts")
        other._dict = fake
        for i in range(3):
            self._put(other, f"old-{i}", i=i)
        fake.data.pop(ModalDictArtifactStore._IDX_GLOBAL_BUILT, None)
        fake.data.pop(ModalDictArtifactStore._IDX_GLOBAL_META, None)
        for key in [k for k in fake.data if k.startswith("index/global/")]:
            fake.data.pop(key)
        store._global_ready = False
        manifests = store.list()
        assert sorted(m.artifact_id for m in manifests) == [f"old-{i}" for i in range(3)]
        assert fake.data[ModalDictArtifactStore._IDX_GLOBAL_BUILT] == b"1"

    def test_delete_removes_global_row(self) -> None:
        store, fake = self._store()
        self._put(store, "art-1", i=1)
        self._put(store, "art-2", i=2)
        store.delete("art-1")
        assert [m.artifact_id for m in store.list()] == ["art-2"]

    def test_stale_global_row_dropped_and_pruned(self) -> None:
        store, fake = self._store()
        self._put(store, "art-1", i=1)
        # Simulate a stale row: index row without a manifest.
        store._global_index_add(
            ArtifactManifest(
                artifact_id="ghost",
                created_at="2026-09-16T00:00:00+00:00",
            )
        )
        fake.put(ModalDictArtifactStore._IDX_GLOBAL_BUILT, b"1")
        manifests = store.list_page(limit=10)
        assert [m.artifact_id for m in manifests.artifacts] == ["art-1"]

    def test_chunk_split_at_max(self, monkeypatch) -> None:
        store, fake = self._store()
        monkeypatch.setattr(ModalDictArtifactStore, "_GLOBAL_CHUNK_MAX", 3)
        for i in range(8):
            self._put(store, f"art-{i}", i=i)
        meta = fake.data[ModalDictArtifactStore._IDX_GLOBAL_META]
        assert meta["chunks"] >= 3
        assert len(store.list()) == 8

    def test_concurrent_puts_keep_index_consistent(self) -> None:
        store, fake = self._store()
        threads = [
            threading.Thread(target=self._put, args=(store, f"art-{i}", "ag", i)) for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(m.artifact_id for m in store.list()) == [f"art-{i}" for i in range(10)]


class TestArtifactGlobalPageCache:
    """SOR-199: ver-gated page cache on the unfiltered ``list_page`` —
    repeat Console navigations cost one token read, writes invalidate
    via the ``index/_global_ver`` bump."""

    def _store(self) -> tuple[ModalDictArtifactStore, _FakeDict]:
        store = ModalDictArtifactStore("test-artifacts")
        fake = _FakeDict()
        store._dict = fake
        return store, fake

    def _put(self, store, artifact_id: str, agent: str = "ag", i: int = 0) -> None:
        import tempfile
        from datetime import timedelta
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "a.txt").write_text("x\n", encoding="utf-8")
            base = datetime(2026, 9, 15, tzinfo=UTC) + timedelta(seconds=i)
            build_artifact(
                ws,
                store=store,
                agent_id=agent,
                artifact_id=artifact_id,
                clock=lambda: base,
            )

    def test_warm_global_page_hits_cache(self) -> None:
        store, fake = self._store()
        for i in range(3):
            self._put(store, f"art-{i}", i=i)
        page = store.list_page(limit=2)
        assert [m.artifact_id for m in page.artifacts] == ["art-0", "art-1"]
        gets = fake.gets
        again = store.list_page(limit=2)
        assert [m.artifact_id for m in again.artifacts] == ["art-0", "art-1"]
        assert fake.gets - gets == 1  # the ``index/_global_ver`` read only

    def test_put_bumps_global_ver_and_invalidates(self) -> None:
        store, fake = self._store()
        self._put(store, "art-1", i=1)
        store.list_page(limit=10)
        ver_before = fake.data[ModalDictArtifactStore._IDX_GLOBAL_VER]
        self._put(store, "art-2", i=2)
        assert fake.data[ModalDictArtifactStore._IDX_GLOBAL_VER] != ver_before
        page = store.list_page(limit=10)
        assert [m.artifact_id for m in page.artifacts] == ["art-1", "art-2"]

    def test_delete_bumps_global_ver(self) -> None:
        store, fake = self._store()
        self._put(store, "art-1", i=1)
        store.list_page(limit=10)
        ver_before = fake.data[ModalDictArtifactStore._IDX_GLOBAL_VER]
        store.delete("art-1")
        assert fake.data[ModalDictArtifactStore._IDX_GLOBAL_VER] != ver_before
        assert store.list_page(limit=10).artifacts == ()

    def test_ttl_expiry_serves_stale_and_revalidates(self, monkeypatch) -> None:
        import control.artifacts as art_mod

        monkeypatch.setattr(art_mod, "_LIST_CACHE_TTL_S", -1.0)
        store, fake = self._store()
        self._put(store, "art-1", i=1)
        store.list_page(limit=10)  # warm
        # Lost-bump write: manifest + global index row, no ``ver`` bump.
        ghost = ArtifactManifest(artifact_id="art-2", created_at="2026-09-16T00:00:00+00:00")
        fake.put("art-2/manifest", manifest_dumps(ghost))
        store._global_index_add(ghost)
        page = store.list_page(limit=10)
        assert [m.artifact_id for m in page.artifacts] == ["art-1"]  # stale serve
        store._refresh_thread.join(timeout=5)
        page = store.list_page(limit=10)
        assert [m.artifact_id for m in page.artifacts] == ["art-1", "art-2"]

    def test_ver_read_failure_serves_stale(self) -> None:
        store, fake = self._store()
        self._put(store, "art-1", i=1)
        store.list_page(limit=10)
        keys_calls = fake.keys_calls  # one-time migration rebuild
        fake.fail_get = True
        page = store.list_page(limit=10)
        assert [m.artifact_id for m in page.artifacts] == ["art-1"]
        assert fake.keys_calls == keys_calls  # masked by cache, no enumeration
        store._refresh_thread.join(timeout=5)
        fake.fail_get = False
        assert [m.artifact_id for m in store.list_page(limit=10).artifacts] == ["art-1"]


class TestKnownRunNsDedupe:
    def test_ledger_run_states_skips_duplicate_list(self) -> None:
        from control.api_v1.lifecycle import LedgerRunStates
        from control.api_v1.routes import _known_run_ns
        from control.run_store import InMemoryRunStore

        class CountingStore(InMemoryRunStore):
            calls = 0

            def list(self, agent_id):
                self.calls += 1
                return super().list(agent_id)

        backing = CountingStore()
        ledger = RunLedger(backing)
        ledger.begin(agent_id="ag", n=1, status="RUNNING")
        run_states = LedgerRunStates(ledger)
        rec = _session("ag")
        ns = _known_run_ns(rec, ledger, run_states)
        assert 1 in ns
        assert backing.calls == 1  # one store pass, not two

    def test_independent_run_states_still_listed(self) -> None:
        from control.api_v1.lifecycle import InMemoryRunStates
        from control.api_v1.routes import _known_run_ns
        from control.run_store import InMemoryRunStore

        ledger = RunLedger(InMemoryRunStore())
        ledger.begin(agent_id="ag", n=1, status="RUNNING")
        run_states = InMemoryRunStates()
        run_states.begin("ag", 2, status="CREATING")
        ns = _known_run_ns(_session("ag"), ledger, run_states)
        assert ns == {1, 2}


class TestSessionListingCache:
    """SOR-199: ver-gated read-through cache on ``list_all`` — steady-state
    listings cost one token read, writes invalidate via the ``ver`` bump."""

    def _store(self) -> tuple[ModalDictStore, _FakeDict, _FakeDict]:
        store = ModalDictStore("test-sessions")
        main, index = _FakeDict(), _FakeDict()
        store._dict = main
        store._index = index
        return store, main, index

    def test_warm_listing_hits_cache(self) -> None:
        store, main, index = self._store()
        for i in range(5):
            store.put(_session(f"ag-{i}"))
        store.list_all()
        main.gets = 0
        index.gets = 0
        records = store.list_all()
        assert sorted(r.id for r in records) == [f"ag-{i}" for i in range(5)]
        assert main.gets == 0  # no record fetches at all
        assert index.gets == 1  # the single ``ver`` token read

    def test_put_bumps_ver_and_invalidates(self) -> None:
        store, main, index = self._store()
        store.put(_session("a"))
        store.list_all()
        ver_before = index.data[ModalDictStore._IDX_VER]
        store.put(_session("b"))
        assert index.data[ModalDictStore._IDX_VER] != ver_before
        assert sorted(r.id for r in store.list_all()) == ["a", "b"]

    def test_put_same_id_bumps_ver(self) -> None:
        store, main, index = self._store()
        store.put(_session("a"))
        store.list_all()
        ver_before = index.data[ModalDictStore._IDX_VER]
        rec = _session("a")
        rec.status = "running"
        store.put(rec)  # id already indexed — content update still bumps
        assert index.data[ModalDictStore._IDX_VER] != ver_before
        assert store.list_all()[0].status == "running"

    def test_delete_bumps_ver_and_invalidates(self) -> None:
        store, main, index = self._store()
        store.put(_session("a"))
        store.put(_session("b"))
        store.list_all()
        ver_before = index.data[ModalDictStore._IDX_VER]
        store.delete("a")
        assert index.data[ModalDictStore._IDX_VER] != ver_before
        assert [r.id for r in store.list_all()] == ["b"]

    def test_cached_records_are_independent_copies(self) -> None:
        store, main, _index = self._store()
        store.put(_session("a"))
        store.list_all()
        rec = store.list_all()[0]  # served from cache
        rec.sandbox_tags["provider"] = "corrupted"
        rec.messages.append({"role": "x"})
        fresh = store.list_all()[0]
        assert fresh.sandbox_tags["provider"] == "codex"
        assert fresh.messages == []

    def test_ttl_expiry_serves_stale_and_revalidates(self, monkeypatch) -> None:
        import control.store as store_mod

        monkeypatch.setattr(store_mod, "_LIST_CACHE_TTL_S", -1.0)
        store, main, index = self._store()
        store.put(_session("a"))
        store.list_all()
        # Lost-bump: a cross-container write that updated the record and
        # the id manifest but crashed before the ``ver`` bump.
        rec = _session("b")
        main.put(rec.id, record_to_dict(rec))
        ids = index.get(ModalDictStore._IDX_IDS)
        index.put(ModalDictStore._IDX_IDS, sorted([*ids, "b"]))
        # Expired page is served stale — the caller never blocks on the
        # fanout — while a background refresh repairs the lost bump.
        assert [r.id for r in store.list_all()] == ["a"]
        store._refresh_thread.join(timeout=5)
        assert sorted(r.id for r in store.list_all()) == ["a", "b"]

    def test_put_merges_into_warm_cache(self) -> None:
        store, main, index = self._store()
        store.put(_session("a"))
        store.list_all()
        store.put(_session("b"))
        main.gets = 0
        index.gets = 0
        records = store.list_all()
        assert sorted(r.id for r in records) == ["a", "b"]
        assert main.gets == 0  # same-container write folded into the page
        assert index.gets == 1  # the single ``ver`` token read

    def test_delete_merges_into_warm_cache(self) -> None:
        store, main, index = self._store()
        store.put(_session("a"))
        store.put(_session("b"))
        store.list_all()
        store.delete("a")
        main.gets = 0
        index.gets = 0
        assert [r.id for r in store.list_all()] == ["b"]
        assert main.gets == 0
        assert index.gets == 1

    def test_ver_read_failure_serves_stale(self) -> None:
        store, main, index = self._store()
        store.put(_session("a"))
        store.list_all()
        index.fail_get = True
        records = store.list_all()
        assert [r.id for r in records] == ["a"]
        assert main.items_calls == 0  # masked by cache, no enumeration
        store._refresh_thread.join(timeout=5)
        index.fail_get = False
        assert [r.id for r in store.list_all()] == ["a"]

    def test_pre_cache_deploy_mints_ver(self) -> None:
        store, main, index = self._store()
        rec = _session("old")
        main.put(rec.id, record_to_dict(rec))
        # Pre-cache-deploy index: ids + built, no version token.
        index.put(ModalDictStore._IDX_IDS, ["old"])
        index.put(ModalDictStore._IDX_BUILT, b"1")
        assert [r.id for r in store.list_all()] == ["old"]
        assert index.data[ModalDictStore._IDX_VER] is not None
        main.gets = 0
        assert [r.id for r in store.list_all()] == ["old"]
        assert main.gets == 0

    def test_rebuild_bumps_ver(self) -> None:
        store, _main, index = self._store()
        store.put(_session("a"))
        store.list_all()
        ver_before = index.data[ModalDictStore._IDX_VER]
        store.rebuild_index()
        assert index.data[ModalDictStore._IDX_VER] != ver_before


class TestWorkflowScanCache:
    """SOR-199: ver-gated read-through cache on ``_iter_agent_raws`` —
    ``all_bindings``/``list_workflow`` stop refetching every wf-agent row."""

    def _store(self) -> tuple[ModalDictWorkflowStore, _FakeDict]:
        store = ModalDictWorkflowStore("test-workflows")
        fake = _FakeDict()
        store._dict = fake
        return store, fake

    def _attach(self, store, agent_id: str, wf: str = "wf-1") -> None:
        store.attach(
            WorkflowTaskRecord(
                owner="key-1",
                workflow_id=wf,
                task_id=f"t-{agent_id}",
                role="worker",
                agent_id=agent_id,
            )
        )

    def test_warm_scan_hits_cache(self) -> None:
        store, fake = self._store()
        for i in range(4):
            self._attach(store, f"ag-{i}")
        store.all_bindings()
        gets = fake.gets
        bindings = store.all_bindings()
        assert sorted(bindings) == [f"ag-{i}" for i in range(4)]
        # manifest + ver reads only — zero per-agent fetches
        assert fake.gets - gets <= 2

    def test_attach_bumps_ver_and_invalidates(self) -> None:
        store, fake = self._store()
        self._attach(store, "ag-1")
        store.all_bindings()
        ver_before = fake.data[ModalDictWorkflowStore._VER_KEY]
        self._attach(store, "ag-2")
        assert fake.data[ModalDictWorkflowStore._VER_KEY] != ver_before
        assert sorted(store.all_bindings()) == ["ag-1", "ag-2"]

    def test_list_workflow_hits_cached_scan(self) -> None:
        store, fake = self._store()
        self._attach(store, "ag-1", "wf-a")
        self._attach(store, "ag-2", "wf-b")
        store.all_bindings()  # warm
        gets = fake.gets
        tasks = store.list_workflow("key-1", "wf-a")
        assert [t.agent_id for t in tasks] == ["ag-1"]
        # manifest + ver + workflow index reads — no per-agent fetches
        assert fake.gets - gets <= 3

    def test_ttl_expiry_serves_stale_and_revalidates(self, monkeypatch) -> None:
        import control.workflow_store as wf_mod

        monkeypatch.setattr(wf_mod, "_LIST_CACHE_TTL_S", -1.0)
        store, fake = self._store()
        self._attach(store, "ag-1")
        store.all_bindings()
        # Lost-bump write: agent record + manifest id, no ``ver`` bump.
        fake.put(
            "wf-agent/ag-2",
            {
                "owner": "key-1",
                "workflow_id": "wf-1",
                "task_id": "t-ag-2",
                "role": "worker",
                "agent_id": "ag-2",
                "created_at": "2026-09-20T00:00:00+00:00",
                "updated_at": "2026-09-20T00:00:00+00:00",
            },
        )
        ids = fake.get(ModalDictWorkflowStore._AGENTS_MANIFEST)
        fake.put(ModalDictWorkflowStore._AGENTS_MANIFEST, sorted([*ids, "ag-2"]))
        assert sorted(store.all_bindings()) == ["ag-1"]  # stale serve
        store._refresh_thread.join(timeout=5)
        assert sorted(store.all_bindings()) == ["ag-1", "ag-2"]

    def test_attach_merges_into_warm_scan(self) -> None:
        store, fake = self._store()
        self._attach(store, "ag-1")
        store.all_bindings()
        self._attach(store, "ag-2")
        gets = fake.gets
        assert sorted(store.all_bindings()) == ["ag-1", "ag-2"]
        assert fake.gets - gets == 1  # the single ``ver`` token read
