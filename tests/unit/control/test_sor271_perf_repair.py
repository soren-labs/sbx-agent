"""SOR-271 production-gate repair invariants.

Pins the two SOR-268-attributed failures the production API/perf gate
returned: ``POST /v2/sessions`` must not run a ``modal_dict.items`` full
scan on ``sbx-accounts`` inside ``resolve_execution`` (indexed account
listing + one-batch writes), the create request path must overlap the
durable reads it can overlap (idempotency replay lookup ∥ owner-doc
prefetch), and warm ``RunLedger`` listings must not pay an index-doc
round trip every call (SSE status pollers call them every 0.5s per
session — the per-tick op pressure behind the >5s read spikes under
fanout).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from control.accounts import (
    InMemoryAccountStore,
    ModalDictAccountStore,
    PersistentAccountRegistry,
    account_to_dict,
)
from control.ports import Account
from control.run_store import ModalDictRunStore, RunRecord
from control.tasks import ModalDictTaskStore, TaskRecord
from tests.unit.control.test_sor268_async_perf import _BatchDict


def _account(account_id: str, provider: str = "codex") -> Account:
    return Account(
        id=account_id,
        provider=provider,
        label=f"{account_id} label",
        models=(),
        max_concurrent=8,
        secret_name="sbx-test",
        created_at=datetime(2026, 9, 29, tzinfo=UTC).isoformat(),
    )


def _account_store() -> tuple[ModalDictAccountStore, _BatchDict]:
    store = ModalDictAccountStore("test-accounts")
    fake = _BatchDict()
    store._dict = fake
    return store, fake


def _run(agent_id: str, n: int) -> RunRecord:
    return RunRecord(
        agent_id=agent_id,
        n=n,
        status="RUNNING",
        created_at="2026-09-29T00:00:00+00:00",
        updated_at="2026-09-29T00:00:00+00:00",
    )


class TestAccountIndex:
    def test_put_writes_row_and_index_in_one_batch(self) -> None:
        store, fake = _account_store()
        store.put_record("a1", account_to_dict(_account("a1")))
        assert fake.items_calls == 0
        assert fake.updates == 1  # account/<id> + __accounts__ atomically
        assert fake.data["account/a1"]["id"] == "a1"
        assert fake.data["__accounts__"]["a1"]["provider"] == "codex"

    def test_list_records_is_one_index_get(self) -> None:
        store, fake = _account_store()
        for i in range(5):
            store.put_record(f"a{i}", account_to_dict(_account(f"a{i}")))
        fake.gets = 0
        records = dict(store.list_records())
        assert sorted(records) == [f"a{i}" for i in range(5)]
        assert fake.gets == 1
        assert fake.items_calls == 0

    def test_registry_list_uses_index_not_items(self) -> None:
        store, fake = _account_store()
        registry = PersistentAccountRegistry(store)
        registry.put(_account("a1"))
        registry.put(_account("a2", provider="grok"))
        fake.gets = 0
        assert [a.id for a in registry.list("codex")] == ["a1"]
        assert [a.id for a in registry.list()] == ["a1", "a2"]
        assert fake.items_calls == 0

    def test_pre_index_dict_falls_back_once_then_self_heals(self) -> None:
        store, fake = _account_store()
        # Simulate a Dict that predates the index: account rows only.
        for i in range(3):
            fake.data[f"account/old{i}"] = account_to_dict(_account(f"old{i}"))
        records = dict(store.list_records())
        assert sorted(records) == [f"old{i}" for i in range(3)]
        assert fake.items_calls == 1  # one migration scan
        assert "__accounts__" in fake.data  # index doc healed
        fake.items_calls = 0
        fake.gets = 0
        assert sorted(dict(store.list_records())) == [f"old{i}" for i in range(3)]
        assert fake.gets == 1 and fake.items_calls == 0

    def test_delete_removes_index_entry_first(self) -> None:
        store, fake = _account_store()
        store.put_record("a1", account_to_dict(_account("a1")))
        store.delete_record("a1")
        assert "a1" not in fake.data["__accounts__"]
        assert "account/a1" not in fake.data
        assert dict(store.list_records()) == {}

    def test_mark_status_keeps_index_fresh(self) -> None:
        store, fake = _account_store()
        registry = PersistentAccountRegistry(store)
        registry.put(_account("a1"))
        registry.mark_status("a1", "disabled")
        fake.items_calls = 0
        fake.gets = 0
        (account,) = registry.list("codex")
        assert account.status == "disabled"
        assert fake.items_calls == 0

    def test_index_get_failure_falls_back_to_scan(self) -> None:
        store, fake = _account_store()
        fake.data["account/a1"] = account_to_dict(_account("a1"))
        fake.fail_get = True
        records = dict(store.list_records())
        assert list(records) == ["a1"]  # items() fallback still answers

    def test_in_memory_store_answers_list_records(self) -> None:
        store = InMemoryAccountStore()
        store.put_record("a1", account_to_dict(_account("a1")))
        assert dict(store.list_records())["a1"]["id"] == "a1"


class TestRunIndexReadCache:
    def _store(self) -> tuple[ModalDictRunStore, _BatchDict, _BatchDict]:
        store = ModalDictRunStore("test-runs")
        data, index = _BatchDict(), _BatchDict()
        index.data["built"] = b"1"
        store._dict = data
        store._index = index
        return store, data, index

    def test_warm_lists_share_one_index_read(self) -> None:
        store, _data, index = self._store()
        store._index_ready = True
        store.list("agent-1")
        store.list("agent-1")
        store.list("agent-2")
        # agent-1 paid one index-doc read; its second list and agent-2's
        # own cached entry are free.
        assert index.gets == 2

    def test_list_fresh_bypasses_the_index_cache(self) -> None:
        store, _data, index = self._store()
        store._index_ready = True
        store.list("agent-1")
        hits = index.gets
        store.list_fresh("agent-1")
        assert index.gets == hits + 1

    def test_put_write_through_keeps_list_warm(self) -> None:
        store, _data, index = self._store()
        store._index_ready = True
        store.put(_run("agent-1", 1))
        hits = index.gets
        records = store.list("agent-1")
        assert [r.n for r in records] == [1]
        assert index.gets == hits  # list hit the write-through cache

    def test_expired_index_cache_refetches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store, _data, index = self._store()
        store._index_ready = True
        monkeypatch.setattr("control.run_store._RUN_IDX_CACHE_TTL_S", 0.0)
        store.list("agent-1")
        store.list("agent-1")
        assert index.gets == 2


class TestCreatePathPrefetch:
    def _task(self, task_id: str, owner: str = "key_1") -> TaskRecord:
        return TaskRecord(
            id=task_id,
            owner=owner,
            status="queued",
            request={"prompt": {"text": "x"}},
            resolved={"execution": {"provider": "codex"}},
            agent_id=None,
            run_id=None,
            created_at="2026-09-29T00:00:00+00:00",
            updated_at="2026-09-29T00:00:00+00:00",
            transitions=[],
            idempotency=None,
        )

    def test_prefetched_owner_doc_skips_the_put_read(self) -> None:
        store, fake = self._stores()
        store.prefetch_owner("key_1")
        store.put(self._task("sess_a"))
        # Owner-doc read already paid by the prefetch (concurrent with
        # find_by_idempotency on the route); put reads only __owners__.
        assert fake.gets == 2
        assert fake.data["owner/key_1"]["ids"] == ["sess_a"]

    def test_prefetch_is_consumed_once(self) -> None:
        store, fake = self._stores()
        store.prefetch_owner("key_1")
        store.put(self._task("sess_a"))
        fake.gets = 0
        store.put(self._task("sess_b"))
        assert fake.gets == 1  # owner-doc read again — no stale replay

    def test_stale_prefetch_falls_back_to_fresh_read(self) -> None:
        store, fake = self._stores()
        store.prefetch_owner("key_1")
        # Another writer lands a row the prefetch cannot see.
        fake.data["owner/key_1"] = {"ids": ["other"], "records": {}}
        store._owner_prefetch["key_1"] = (
            __import__("time").monotonic() - 10.0,
            fake.data["owner/key_1"],
        )
        store.put(self._task("sess_a"))
        assert "other" in fake.data["owner/key_1"]["ids"]

    def test_prefetch_failure_is_invisible(self) -> None:
        store, fake = self._stores()
        fake.fail_get = True
        store.prefetch_owner("key_1")  # swallowed
        fake.fail_get = False
        store.put(self._task("sess_a"))
        assert fake.data["owner/key_1"]["ids"] == ["sess_a"]

    def test_concurrent_find_and_prefetch_overlap(self) -> None:
        """The route's shape: find_by_idempotency ∥ prefetch_owner — the
        serial chain the production gate counted loses one round trip."""
        store, fake = self._stores()
        fake.data["owner/key_1"] = {"ids": ["prior"], "records": {}}
        with ThreadPoolExecutor(max_workers=2) as pool:
            prior_fut = pool.submit(store.find_by_idempotency, "key_1", "k")
            pool.submit(store.prefetch_owner, "key_1")
            prior = prior_fut.result()
        assert prior is None
        store.put(self._task("sess_a"))
        assert "prior" in fake.data["owner/key_1"]["ids"]

    def _stores(self) -> tuple[ModalDictTaskStore, _BatchDict]:
        store = ModalDictTaskStore("test-tasks")
        fake = _BatchDict()
        store._dict = fake
        return store, fake
