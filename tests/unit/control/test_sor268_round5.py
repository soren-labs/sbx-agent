"""SOR-268 round 5: list-path self-heal for missing owner-doc summaries.

v23 production gate: ``GET /v2/sessions`` regressed ~8x (~12-14s @368-400
rows) — every pre-index or agent-less owner-doc entry fell into the
``missing`` path and was point-read on *every* list call forever. The
Dict accumulates such rows across deploys, so the per-list point-read
count grew with total history. The fix: terminal summaries are trusted
(they cannot wedge forward) and fetched rows are merged back into the
owner doc so the point reads are paid once, not per call.
"""

from __future__ import annotations

from control.tasks import ModalDictTaskStore, _task_summary
from tests.unit.control.test_sor268_async_perf import _BatchDict
from tests.unit.control.test_sor268_manual_final import _modal_task_store
from tests.unit.control.test_sor271_round2 import _task


def _seed_missing(store: ModalDictTaskStore, fake: _BatchDict, task_id: str, status: str) -> None:
    """Insert ``task/<id>`` keyed in ``ids`` but absent from the owner
    doc's ``records`` map — the shape ``missing`` catches: pre-index rows
    accumulated by older deploys, entries dropped by a lost owner-doc
    update. Immutable key metadata is absent too, as in a legacy deploy."""
    rec = _task(task_id, status=status, agent_id="agent-x")
    store.put(rec)
    doc = fake.data[f"owner/{rec.owner}"]
    doc["records"].pop(task_id, None)
    doc.get("idempotency_keys", {}).pop(task_id, None)
    store._owner_list_cache.clear()


def _expire_memo(store: ModalDictTaskStore) -> None:
    """Next ``list`` pays the doc read again (the 1s memo TTL lapsed)."""
    store._owner_list_cache.clear()


class TestMissingSummaryBackfill:
    def test_missing_rows_are_point_read_once_then_backfilled(self) -> None:
        store, fake = _modal_task_store()
        for i in range(6):
            _seed_missing(store, fake, f"sess_{i}", "finished")
        fake.gets = 0

        first = store.list("key_1")
        first_gets = fake.gets
        _expire_memo(store)
        fake.gets = 0
        second = store.list("key_1")

        assert len(first) == len(second) == 6
        # First listing: owner-doc get + one point get per missing row +
        # the backfill's re-read of the doc it merges into.
        assert first_gets == 8
        # Second listing: backfill healed the doc — one owner-doc get, no
        # per-row reads (the v23 regression was this staying ~N per call).
        assert fake.gets == 1

    def test_backfilled_summaries_match_put_shape(self) -> None:
        store, fake = _modal_task_store()
        _seed_missing(store, fake, "sess_0", "finished")
        first = store.list("key_1")
        rec = first[0]
        summary = fake.data["owner/key_1"]["records"]["sess_0"]
        assert summary == _task_summary(rec)
        assert summary.get("response") is None

    def test_backfill_is_merge_only(self) -> None:
        store, fake = _modal_task_store()
        _seed_missing(store, fake, "sess_0", "finished")
        # A concurrent-looking entry already present must survive.
        fake.data["owner/key_1"]["ids"].append("sess_keep")
        fake.data["owner/key_1"]["records"]["sess_keep"] = _task_summary(
            _task("sess_keep", status="running", agent_id="agent-k")
        )
        store.list("key_1")
        doc = fake.data["owner/key_1"]
        assert "sess_keep" in doc["ids"]
        assert doc["records"]["sess_keep"]["status"] == "running"
        assert "sess_0" in doc["records"]

    def test_backfill_recovers_from_write_failure(self) -> None:
        store, fake = _modal_task_store()
        _seed_missing(store, fake, "sess_0", "finished")

        class _FailOnceDict(_BatchDict):
            def __init__(self, inner: _BatchDict) -> None:
                super().__init__()
                self.data = inner.data
                self.boomed = False

            def update(self, mapping):
                if not self.boomed:
                    self.boomed = True
                    raise RuntimeError("transient")
                super().update(mapping)

        store._dict = _FailOnceDict(fake)
        first = store.list("key_1")
        assert len(first) == 1  # read still answered; heal deferred
        _expire_memo(store)
        second = store.list("key_1")
        assert len(second) == 1
        _expire_memo(store)
        store._dict.gets = 0
        store.list("key_1")
        # After the successful heal on call 2, call 3 is a single doc read.
        assert store._dict.gets == 1


class TestFindByIdempotencyBackfill:
    def test_keyed_lookup_heals_missing_rows(self) -> None:
        """The v23 create-ACK regression: every keyed create walked the
        pre-index/missing set point-wise — ~N gets per call, forever."""
        store, fake = _modal_task_store()
        for i in range(6):
            _seed_missing(store, fake, f"sess_{i}", "finished")
        fake.gets = 0

        first = store.find_by_idempotency("key_1", "v2:session:k")
        first_gets = fake.gets
        fake.gets = 0
        second = store.find_by_idempotency("key_1", "v2:session:k")

        assert first is None and second is None
        # idem point get + owner-doc get + per-missing point read + the
        # backfill's doc re-read.
        assert first_gets == 9
        # idem point get + owner-doc get — healed summaries answer.
        assert fake.gets == 2

    def test_heal_happens_even_on_match(self) -> None:
        store, fake = _modal_task_store()
        pinned = _task("sess_pin", status="finished", agent_id="agent-p")
        pinned.idempotency = {"key_id": "key_1", "key": "v2:session:k", "fingerprint": "f"}
        store.put(pinned)
        # Make the pin pre-index: no idem point row, no owner-doc summary
        # — the match must walk the missing set.
        idem_key = next(k for k in fake.data if k.startswith("idem/"))
        fake.data.pop(idem_key)
        fake.data["owner/key_1"]["records"].pop("sess_pin", None)
        fake.data["owner/key_1"].get("idempotency_keys", {}).pop("sess_pin", None)
        _seed_missing(store, fake, "sess_0", "finished")
        store._owner_list_cache.clear()

        hit = store.find_by_idempotency("key_1", "v2:session:k")
        assert hit is not None and hit.id == "sess_pin"
        doc = fake.data["owner/key_1"]
        # The whole fetched set was healed, not just the match.
        assert "sess_0" in doc["records"]
        assert "sess_pin" in doc["records"]

    def test_heal_survives_the_create_put(self) -> None:
        """``put`` consumes the staged prefetch doc — it must merge onto
        the healed doc, not a pre-heal read. Without the prefetch
        re-stage every keyed create clobbered the backfill and re-walked
        the missing set (the v23 create-ACK regression)."""
        store, fake = _modal_task_store()
        for i in range(6):
            _seed_missing(store, fake, f"sess_{i}", "finished")
        assert store.find_by_idempotency("key_1", "v2:session:k") is None

        new = _task("sess_new", status="queued", agent_id=None)
        store.put(new)

        doc = fake.data["owner/key_1"]
        for i in range(6):
            assert f"sess_{i}" in doc["records"]
        assert "sess_new" in doc["records"]
        fake.gets = 0
        store.find_by_idempotency("key_1", "v2:session:k")
        # idem point get + owner-doc get — the healed doc answers.
        assert fake.gets == 2


class TestSummaryTrust:
    def test_terminal_agentless_summary_is_trusted(self) -> None:
        store, fake = _modal_task_store()
        rec = _task("sess_0", status="finished", agent_id=None)
        store.put(rec)
        _expire_memo(store)
        fake.gets = 0

        rows = store.list("key_1")

        assert len(rows) == 1 and rows[0].status == "finished"
        # One owner-doc read; the point-read distrust no longer applies
        # to a summary that already reports terminal.
        assert fake.gets == 2  # owner + bounded terminal validation

    def test_live_agentless_summary_still_point_reads(self) -> None:
        store, fake = _modal_task_store()
        rec = _task("sess_0", status="queued", agent_id=None)
        store.put(rec)
        _expire_memo(store)
        fake.gets = 0

        rows = store.list("key_1")

        assert len(rows) == 1
        # Owner-doc get + authoritative row read — the SOR-271 wedge
        # protection is kept for rows that can still transition. The
        # fresh-but-untrusted summary is not written back (it would be
        # re-read next call anyway).
        assert fake.gets == 2

    def test_agent_bound_summary_skips_point_read(self) -> None:
        store, fake = _modal_task_store()
        store.put(_task("sess_0", status="running", agent_id="agent-a"))
        _expire_memo(store)
        fake.gets = 0

        rows = store.list("key_1")

        assert len(rows) == 1
        assert fake.gets == 1


class TestListOpBoundAtScale:
    def test_history_shape_stays_one_get_after_heal(self) -> None:
        """The production shape: ~400 accumulated rows, most pre-index —
        after the first healing list, a warm list is a single remote op
        plus the short memo TTL, never O(history) point reads."""
        store, fake = _modal_task_store()
        for i in range(200):
            _seed_missing(store, fake, f"sess_{i:03d}", "finished" if i % 3 else "cancelled")
        for i in range(5):
            store.put(_task(f"sess_live_{i}", status="running", agent_id=f"agent-l{i}"))
        # First list heals the whole tail.
        first = store.list("key_1")
        assert len(first) == 205
        _expire_memo(store)
        fake.gets = 0
        store.list("key_1")
        assert fake.gets == 1
