"""Production-shaped regressions for the round-5 performance gate."""

from control.tasks import ModalDictTaskStore
from tests.acceptance.v2_perf_gate import ProbeResult, _percentile
from tests.unit.control.test_sor268_async_perf import _BatchDict
from tests.unit.control.test_sor271_round2 import _task


def test_small_sample_p95_does_not_hide_slow_request() -> None:
    for samples in ([0.2, 1.6], [0.2, 0.3, 1.6]):
        result = ProbeResult("ACK", list(samples), 1.0)
        assert result.p95 == 1.6
        assert not result.ok()
        assert _percentile(list(samples), 0.95) == 1.6


def test_unbound_terminal_history_uses_one_owner_read() -> None:
    store = ModalDictTaskStore("test-tasks")
    fake = _BatchDict()
    store._dict = fake
    for i in range(400):
        store.put(_task(f"sess_{i}", status="error" if i % 2 else "cancelled", agent_id=None))
    fake.gets = 0

    records = store.list("key_1")

    assert len(records) == 400
    assert fake.gets == 1
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
    assert fake.gets == 3  # owner + authoritative row + merge-only backfill


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
