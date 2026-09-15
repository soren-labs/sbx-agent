"""Durable run ledger: persistence, terminal monotonicity, re-open, corrupt."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from control.run_store import (
    TERMINAL_RUN_STATUSES,
    FileRunStore,
    InMemoryRunStore,
    RunLedger,
    outcome_from_turn_payload,
    record_from_dict,
)


def _clock(start: datetime | None = None):
    base = start or datetime.now(UTC)
    ticks = iter(range(10_000))
    return lambda: base + timedelta(seconds=next(ticks))


def _stores(tmp_path):
    return [InMemoryRunStore(), FileRunStore(tmp_path / "runs")]


class TestOutcomeMapping:
    def test_success_payload_maps_finished(self) -> None:
        payload = {
            "status": "success",
            "message": "done",
            "usage": {"input_tokens": 3},
            "exit_code": 0,
        }
        status, error, result, usage = outcome_from_turn_payload(payload)
        assert status == "FINISHED"
        assert error is None
        assert result == "done"
        assert usage == {"input_tokens": 3}

    def test_timeout_payload_maps_expired(self) -> None:
        status, error, _, _ = outcome_from_turn_payload({"status": "timeout"})
        assert status == "EXPIRED"
        assert error["code"] == "timeout"

    def test_error_payloads_map_error(self) -> None:
        for turn_status, code in (
            ("auth_invalid", "auth_invalid"),
            ("bad_json", "event_parse_error"),
            ("codex_error", "runtime_error"),
        ):
            status, error, _, _ = outcome_from_turn_payload({"status": turn_status})
            assert status == "ERROR"
            assert error["code"] == code

    def test_missing_payload_is_error_not_success(self) -> None:
        status, error, result, usage = outcome_from_turn_payload(None)
        assert status == "ERROR"
        assert error["code"] == "evidence_unavailable"
        assert result is None
        assert usage is None


class TestLedgerTransitions:
    @pytest.mark.parametrize("store_kind", ["memory", "file"])
    def test_begin_finish_roundtrip(self, tmp_path, store_kind) -> None:
        store = InMemoryRunStore() if store_kind == "memory" else FileRunStore(tmp_path)
        ledger = RunLedger(store, clock=_clock())
        record = ledger.begin(
            agent_id="a1",
            n=1,
            provider="codex",
            account_id="acct-1",
            model="gpt-5.6-luna",
        )
        assert record.status == "RUNNING"
        assert record.started_at
        done = ledger.finish(
            "a1",
            1,
            status="FINISHED",
            result_text="hello",
            usage={"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 2},
        )
        assert done.status == "FINISHED"
        assert done.finished_at
        got = ledger.get("a1", 1)
        assert got is not None
        assert got.status == "FINISHED"
        assert got.result_text == "hello"
        assert got.usage["output_tokens"] == 2
        assert got.provider == "codex"
        assert got.account_id == "acct-1"
        assert "turns/1.json" in got.artifact_refs

    def test_begin_is_idempotent_and_never_resurrects(self, tmp_path) -> None:
        ledger = RunLedger(FileRunStore(tmp_path), clock=_clock())
        first = ledger.begin(agent_id="a1", n=1, provider="codex")
        again = ledger.begin(agent_id="a1", n=1, provider="codex")
        assert again.created_at == first.created_at
        ledger.finish("a1", 1, status="ERROR", error={"code": "runtime_error"})
        resurrected = ledger.begin(agent_id="a1", n=1)
        assert resurrected.status == "ERROR"

    @pytest.mark.parametrize("terminal", sorted(TERMINAL_RUN_STATUSES))
    def test_terminal_states_are_monotonic(self, tmp_path, terminal) -> None:
        ledger = RunLedger(FileRunStore(tmp_path), clock=_clock())
        ledger.begin(agent_id="a1", n=1)
        settled = ledger.finish("a1", 1, status=terminal, result_text="final")
        assert settled.status == terminal
        # No subsequent transition may rewrite a persisted terminal record.
        for status in ("FINISHED", "ERROR", "CANCELLED", "EXPIRED"):
            after = ledger.finish("a1", 1, status=status, result_text="other")
            assert after.status == terminal
        cancelled = ledger.cancel("a1", 1)
        assert cancelled.status == terminal
        assert ledger.get("a1", 1).result_text == "final"

    def test_cancel_beats_late_finish(self, tmp_path) -> None:
        ledger = RunLedger(InMemoryRunStore(), clock=_clock())
        ledger.begin(agent_id="a1", n=2)
        cancelled = ledger.cancel("a1", 2)
        assert cancelled.status == "CANCELLED"
        assert cancelled.error["code"] == "cancelled"
        late = ledger.finish("a1", 2, status="FINISHED", result_text="too late")
        assert late.status == "CANCELLED"

    def test_finish_rejects_open_status(self, tmp_path) -> None:
        ledger = RunLedger(FileRunStore(tmp_path))
        with pytest.raises(ValueError):
            ledger.finish("a1", 1, status="RUNNING")
        with pytest.raises(ValueError):
            ledger.begin(agent_id="a1", n=1, status="FINISHED")

    def test_discard_only_removes_open_records(self, tmp_path) -> None:
        ledger = RunLedger(FileRunStore(tmp_path), clock=_clock())
        ledger.begin(agent_id="a1", n=1)
        ledger.discard("a1", 1)
        assert ledger.get("a1", 1) is None
        ledger.begin(agent_id="a1", n=2)
        ledger.finish("a1", 2, status="FINISHED")
        ledger.discard("a1", 2)
        assert ledger.get("a1", 2).status == "FINISHED"

    def test_mark_running(self, tmp_path) -> None:
        ledger = RunLedger(InMemoryRunStore(), clock=_clock())
        record = ledger.begin(agent_id="a1", n=1, status="CREATING")
        assert record.started_at is None
        running = ledger.mark_running("a1", 1)
        assert running.status == "RUNNING"
        assert running.started_at
        ledger.finish("a1", 1, status="ERROR")
        assert ledger.mark_running("a1", 1).status == "ERROR"


class TestDurability:
    def test_file_store_reopen_reads_records(self, tmp_path) -> None:
        root = tmp_path / "runs"
        ledger1 = RunLedger(FileRunStore(root), clock=_clock())
        ledger1.begin(agent_id="a1", n=1, provider="grok", account_id="acct-9")
        ledger1.finish(
            "a1",
            1,
            status="ERROR",
            error={"code": "runtime_error", "message": "boom"},
            usage={"output_tokens": 7},
        )
        # Simulated control-plane restart: a new ledger over the same dir.
        ledger2 = RunLedger(FileRunStore(root))
        got = ledger2.get("a1", 1)
        assert got is not None
        assert got.status == "ERROR"
        assert got.error["code"] == "runtime_error"
        assert got.usage == {"output_tokens": 7}
        assert got.provider == "grok"
        assert [r.n for r in ledger2.list("a1")] == [1]

    def test_missing_record_is_none(self, tmp_path) -> None:
        assert FileRunStore(tmp_path).get("ghost", 1) is None
        assert InMemoryRunStore().get("ghost", 1) is None

    def test_corrupt_file_decodes_to_unknown(self, tmp_path) -> None:
        root = tmp_path / "runs"
        store = FileRunStore(root)
        path = root / "a1" / "run-1.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        record = store.get("a1", 1)
        assert record is not None
        assert record.status == "UNKNOWN"
        assert record.error["code"] == "ledger_record_corrupt"
        assert record.terminal is False
        # A corrupt record is still listed, not silently dropped.
        assert [r.n for r in store.list("a1")] == [1]

    def test_corrupt_payload_fields_decode_to_unknown(self) -> None:
        store = InMemoryRunStore()
        store._items["a1/3"] = {"agent_id": "a1", "n": 3, "status": "BOGUS"}
        record = store.get("a1", 3)
        assert record.status == "UNKNOWN"
        assert record.error["code"] == "ledger_record_corrupt"

    def test_record_from_dict_rejects_bad_shapes(self) -> None:
        for bad in (
            "nope",
            {"agent_id": "a1"},
            {"agent_id": "a1", "n": 1, "status": "SUCCEEDED"},
            {"agent_id": "a1", "n": 0, "status": "FINISHED"},
        ):
            with pytest.raises(ValueError):
                record_from_dict(bad)


class TestLeakHygiene:
    def test_record_never_carries_credential_fields(self, tmp_path) -> None:
        ledger = RunLedger(FileRunStore(tmp_path), clock=_clock())
        ledger.begin(agent_id="a1", n=1, provider="codex", account_id="acct-1")
        ledger.finish("a1", 1, status="FINISHED", result_text="ok")
        raw = json.loads((tmp_path / "a1" / "run-1.json").read_text())
        text = json.dumps(raw).lower()
        for marker in ("token", "secret", "password", "credential", "auth"):
            assert marker not in text
