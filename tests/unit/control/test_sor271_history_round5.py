"""Production-shaped regressions for the round-5 performance gate."""

import copy

import pytest
from control.tasks import ModalDictTaskStore
from tests.acceptance.v2_perf_gate import ProbeResult, _percentile
from tests.unit.control.test_sor268_async_perf import _BatchDict
from tests.unit.control.test_sor271_round2 import _task


@pytest.mark.parametrize("lookup", ["prefetch", "idempotency"])
@pytest.mark.parametrize("change", ["put", "delete"])
def test_owner_prefetch_cannot_overwrite_intervening_local_write(lookup, change) -> None:
    store = ModalDictTaskStore("test-tasks")
    fake = _BatchDict()
    store._dict = fake
    store.put(_task("sess_seed", status="error", agent_id=None))
    original = fake.get
    pending = True

    def read_then_write(key, default=None):
        nonlocal pending
        observed = copy.deepcopy(original(key, default))
        if key == "owner/key_1" and pending:
            pending = False
            if change == "put":
                store.put(_task("sess_between", status="error", agent_id=None))
            else:
                store.delete("sess_seed")
        return observed

    fake.get = read_then_write
    if lookup == "prefetch":
        store.prefetch_owner("key_1")
    else:
        store.find_by_idempotency("key_1", "v2:session:next")
    store.put(_task("sess_next", status="error", agent_id=None))

    expected = {"sess_seed", "sess_between", "sess_next"} if change == "put" else {"sess_next"}
    assert set(fake.data["owner/key_1"]["ids"]) == expected
    assert {row.id for row in store.list("key_1")} == expected


def test_small_sample_p95_does_not_hide_slow_request() -> None:
    for samples in ([0.2, 1.6], [0.2, 0.3, 1.6]):
        result = ProbeResult("ACK", list(samples), 1.0)
        assert result.p95 == 1.6
        assert not result.ok()
        assert _percentile(list(samples), 0.95) == 1.6


def test_unbound_terminal_history_has_bounded_validation_reads() -> None:
    store = ModalDictTaskStore("test-tasks")
    fake = _BatchDict()
    store._dict = fake
    for i in range(400):
        store.put(_task(f"sess_{i}", status="error" if i % 2 else "cancelled", agent_id=None))
    fake.gets = 0

    records = store.list("key_1")

    assert len(records) == 400
    assert fake.gets == 9  # one owner doc + eight rotating validation reads
    assert {r.status for r in records} == {"error", "cancelled"}


def test_unbound_queued_rows_still_converge_from_authoritative_record() -> None:
    store = ModalDictTaskStore("test-tasks")
    fake = _BatchDict()
    store._dict = fake
    for i in range(400):
        store.put(_task(f"sess_{i}", status="error", agent_id=None))
    fake.data["owner/key_1"]["records"]["sess_0"]["status"] = "queued"
    fake.gets = 0

    records = store.list("key_1")

    assert len(records) == 400
    assert all(r.status == "error" for r in records)
    assert fake.gets == 11  # owner + queued + eight validations + backfill


def test_retry_publishes_new_unbound_status_and_invalidates_list_cache() -> None:
    store = ModalDictTaskStore("test-tasks")
    fake = _BatchDict()
    store._dict = fake
    record = _task("sess_retry", status="error", agent_id=None)
    store.put(record)
    assert store.list("key_1")[0].status == "error"

    record.status = "queued"
    store.put(record)

    assert store.list("key_1")[0].status == "queued"


def test_backfill_preserves_retry_published_after_point_read() -> None:
    for lookup in ("list", "idempotency"):
        store = ModalDictTaskStore("test-tasks")
        fake = _BatchDict()
        store._dict = fake
        record = _task("sess_retry", status="error", agent_id=None)
        store.put(record)
        fake.data["owner/key_1"]["records"].pop(record.id)
        original = store._pooled_gets

        def fetched_then_retry(ids):
            fetched = original(ids)
            record.status = "queued"
            record.updated_at = "2026-09-30T04:00:00+00:00"
            store.put(record)
            return fetched

        store._pooled_gets = fetched_then_retry
        if lookup == "list":
            store.list("key_1")
        else:
            store.find_by_idempotency("key_1", "v2:session:new")
        store._pooled_gets = original
        store._owner_list_cache.clear()
        assert fake.data["owner/key_1"]["records"][record.id]["status"] == "queued"
        assert store.list("key_1")[0].status == "queued"


def test_lost_cross_instance_retry_summary_eventually_converges() -> None:
    class CopyDict(_BatchDict):
        def get(self, key, default=None):
            return copy.deepcopy(super().get(key, default))

    fake = CopyDict()
    first = ModalDictTaskStore("test-tasks")
    second = ModalDictTaskStore("test-tasks")
    first._dict = second._dict = fake
    for i in range(17):
        first.put(_task(f"sess_{i}", status="error", agent_id=None))
    second.prefetch_owner("key_1")
    retried = _task("sess_16", status="queued", agent_id=None)
    first.put(retried)
    second.put(_task("sess_other", status="finished", agent_id="agent-other"))
    assert fake.data["owner/key_1"]["records"]["sess_16"]["status"] == "error"

    for _ in range(3):
        rows = first.list("key_1")
    assert next(r for r in rows if r.id == "sess_16").status == "queued"
    assert fake.data["owner/key_1"]["records"]["sess_16"]["status"] == "queued"


def test_backfill_does_not_resurrect_deleted_index_entry() -> None:
    """A row deleted between the owner-doc read and the summary backfill
    must not be re-indexed — the healed summary would serve the gone
    record as a phantom row on every later list."""
    for lookup in ("list", "idempotency"):
        store = ModalDictTaskStore("test-tasks")
        fake = _BatchDict()
        store._dict = fake
        record = _task("sess_gone", status="error", agent_id=None)
        store.put(record)
        # Pre-index shape: the id is in the manifest without a summary, so
        # the row is point-read and offered to the backfill.
        fake.data["owner/key_1"]["records"].pop(record.id)
        original = store._pooled_gets

        def fetched_then_deleted(ids):
            fetched = original(ids)
            store.delete(record.id)
            return fetched

        store._pooled_gets = fetched_then_deleted
        try:
            if lookup == "list":
                store.list("key_1")
            else:
                store.find_by_idempotency("key_1", "v2:session:new")
        finally:
            store._pooled_gets = original
        store._owner_list_cache.clear()
        doc = fake.data["owner/key_1"]
        assert record.id not in doc["ids"]
        assert record.id not in doc["records"]
        assert store.list("key_1") == []
