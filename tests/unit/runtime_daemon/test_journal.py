"""Daemon SQLite journal — durable acceptance idempotency + spool
watermarks (RFC 167 §03: same id+body → same status; id+diff → conflict;
spool is the replayable evidence log across restarts)."""

from __future__ import annotations

import pytest
from runtime.daemon.journal import Journal

pytestmark = pytest.mark.unit


@pytest.fixture()
def journal(tmp_path):
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


class TestAccept:
    def test_accept_replay_conflict(self, journal):
        st, prior = journal.accept_operation(
            operation_id="eff_1",
            kind="turn.start",
            session_id="sess_1",
            request_digest="sha256:a",
            envelope={"x": 1},
        )
        assert st == "accepted" and prior is None

        st, prior = journal.accept_operation(
            operation_id="eff_1",
            kind="turn.start",
            session_id="sess_1",
            request_digest="sha256:a",
            envelope={"x": 1},
        )
        assert st == "replay" and prior["state"] == "accepted"

        st, prior = journal.accept_operation(
            operation_id="eff_1",
            kind="turn.start",
            session_id="sess_1",
            request_digest="sha256:DIFFERENT",
            envelope={"x": 2},
        )
        assert st == "conflict" and prior["envelope"] == {"x": 1}

    def test_persists_across_reopen(self, tmp_path):
        path = tmp_path / "j.db"
        j = Journal(path)
        j.accept_operation(
            operation_id="eff_9",
            kind="files.read",
            session_id="s",
            request_digest="sha256:q",
            envelope={},
        )
        j.update_operation("eff_9", "succeeded", {"files": []})
        epoch = j.runtime_epoch
        j.close()

        j2 = Journal(path)
        st, prior = j2.accept_operation(
            operation_id="eff_9",
            kind="files.read",
            session_id="s",
            request_digest="sha256:q",
            envelope={},
        )
        assert st == "replay" and prior["state"] == "succeeded"
        assert prior["result"] == {"files": []}
        # Same DB file → same runtime epoch (journal identity survives).
        assert j2.runtime_epoch == epoch
        assert j2.all_operation_ids() == ["eff_9"]
        j2.close()


class TestSpool:
    def test_append_ack_watermark(self, journal):
        s1 = journal.spool_append("observation", {"kind": "turn_started"})
        s2 = journal.spool_append("observation", {"kind": "item_completed"})
        s3 = journal.spool_append("operation.result", {"state": "succeeded"})
        assert (s1, s2, s3) == (1, 2, 3)

        uncommitted = journal.spool_uncommitted()
        assert [s for s, _, _ in uncommitted] == [1, 2, 3]
        assert journal.spool_max_seq() == 3
        assert journal.committed_watermark() == 0

        journal.spool_ack(2)
        assert [s for s, _, _ in journal.spool_uncommitted()] == [3]
        assert journal.committed_watermark() == 2

        journal.spool_ack(99)
        assert journal.spool_uncommitted() == []
        # Ack is authoritative: upstream asserted everything ≤99 committed.
        assert journal.committed_watermark() == 99
