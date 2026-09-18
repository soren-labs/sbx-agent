"""Durable run ledger: persistence, terminal monotonicity, re-open, corrupt."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from control.run_errors import CODE_CONTRACT_VIOLATION
from control.run_store import (
    TERMINAL_RUN_STATUSES,
    FileRunStore,
    InMemoryRunStore,
    RunLedger,
    apply_output_contract,
    contract_view,
    outcome_from_turn_payload,
    record_from_dict,
)
from runtime.runner.contract import (
    STATUS_INVALID as CONTRACT_INVALID,
)
from runtime.runner.contract import (
    STATUS_PENDING as CONTRACT_PENDING,
)
from runtime.runner.contract import (
    STATUS_SKIPPED as CONTRACT_SKIPPED,
)
from runtime.runner.contract import (
    STATUS_VALID as CONTRACT_VALID,
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
        assert error["code"] == "runtime_error"
        assert error["source"] == "runtime"
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
        assert record.error["code"] == "runtime_error"
        assert record.terminal is False
        # A corrupt record is still listed, not silently dropped.
        assert [r.n for r in store.list("a1")] == [1]

    def test_corrupt_payload_fields_decode_to_unknown(self) -> None:
        store = InMemoryRunStore()
        store._items["a1/3"] = {"agent_id": "a1", "n": 3, "status": "BOGUS"}
        record = store.get("a1", 3)
        assert record.status == "UNKNOWN"
        assert record.error["code"] == "runtime_error"

    def test_record_from_dict_rejects_bad_shapes(self) -> None:
        for bad in (
            "nope",
            {"agent_id": "a1"},
            {"agent_id": "a1", "n": 1, "status": "SUCCEEDED"},
            {"agent_id": "a1", "n": 0, "status": "FINISHED"},
        ):
            with pytest.raises(ValueError):
                record_from_dict(bad)


CONTRACT = {
    "schema": {
        "type": "object",
        "required": ["summary", "ok"],
        "properties": {"summary": {"type": "string"}, "ok": {"type": "boolean"}},
    },
    "enforcement": "strict",
    "schema_digest": "sha256:test",
}
VALID_JSON = '{"summary": "did the thing", "ok": true}'
INVALID_JSON = '{"summary": 3}'  # missing "ok", wrong type
NOT_JSON = "sorry, I could not produce JSON"


class TestOutputContract:
    """SOR-130: ``apply_output_contract`` is the authoritative verdict —
    computed on the control plane from the recorded message, never trusted
    from sandbox payload fields."""

    def test_no_contract_passthrough(self) -> None:
        status, error, structured, meta = apply_output_contract("FINISHED", None, "anything", None)
        assert (status, error, structured, meta) == ("FINISHED", None, None, None)

    def test_valid_output_persists_structured_value(self) -> None:
        status, error, structured, meta = apply_output_contract(
            "FINISHED", None, VALID_JSON, CONTRACT
        )
        assert status == "FINISHED"
        assert error is None
        assert structured == {"summary": "did the thing", "ok": True}
        assert meta["status"] == CONTRACT_VALID
        assert meta["enforcement"] == "strict"
        assert meta["schema_digest"] == "sha256:test"
        assert meta["extraction"] == "raw"
        assert meta["violations"] == []

    def test_strict_schema_violation_flips_to_error(self) -> None:
        status, error, structured, meta = apply_output_contract(
            "FINISHED", None, INVALID_JSON, CONTRACT
        )
        assert status == "ERROR"
        assert error["code"] == CODE_CONTRACT_VIOLATION
        assert error["source"] == "control"
        assert error["retryable"] is True
        assert structured == {"summary": 3}  # extracted, but not valid
        assert meta["status"] == CONTRACT_INVALID
        codes = {v["code"] for v in meta["violations"]}
        assert codes == {"required", "type"}
        # machine-diagnosable fields on every violation
        for v in meta["violations"]:
            assert v["path"].startswith("$")
            assert v["message"]

    def test_strict_malformed_output_is_contract_violation(self) -> None:
        status, error, structured, meta = apply_output_contract(
            "FINISHED", None, NOT_JSON, CONTRACT
        )
        assert status == "ERROR"
        assert error["code"] == CODE_CONTRACT_VIOLATION
        assert "not_json" in error["message"]
        assert structured is None
        assert meta["status"] == CONTRACT_INVALID
        assert meta["extraction"] is None
        assert meta["violations"] == [
            {"path": "$", "code": "not_json", "message": meta["violations"][0]["message"]}
        ]

    def test_warn_keeps_finished_with_diagnostic(self) -> None:
        warn = {**CONTRACT, "enforcement": "warn"}
        status, error, structured, meta = apply_output_contract(
            "FINISHED", None, INVALID_JSON, warn
        )
        assert status == "FINISHED"  # not silently valid — diagnostic attached
        assert error["code"] == CODE_CONTRACT_VIOLATION
        assert meta["status"] == CONTRACT_INVALID
        assert meta["enforcement"] == "warn"
        assert structured == {"summary": 3}

    def test_non_finished_skips_evaluation(self) -> None:
        for terminal in ("ERROR", "CANCELLED", "EXPIRED"):
            status, error, structured, meta = apply_output_contract(
                terminal, {"code": "cancelled"}, VALID_JSON, CONTRACT
            )
            assert status == terminal
            assert error == {"code": "cancelled"}  # original error preserved
            assert structured is None
            assert meta["status"] == CONTRACT_SKIPPED
            assert meta["violations"] == []

    def test_finish_persists_contract_fields(self, tmp_path) -> None:
        ledger = RunLedger(FileRunStore(tmp_path), clock=_clock())
        ledger.begin(agent_id="a1", n=1, provider="codex", output_contract=CONTRACT)
        verdict = {
            "enforcement": "strict",
            "schema_digest": "sha256:test",
            "status": CONTRACT_VALID,
            "extraction": "raw",
            "violations": [],
        }
        ledger.finish(
            "a1",
            1,
            status="FINISHED",
            result_text=VALID_JSON,
            structured_output={"summary": "did the thing", "ok": True},
            contract_result=verdict,
        )
        got = ledger.get("a1", 1)
        assert got.output_contract == CONTRACT
        assert got.structured_output == {"summary": "did the thing", "ok": True}
        assert got.contract_result["status"] == CONTRACT_VALID
        # contract_view renders the persisted verdict.
        view = contract_view(got)
        assert view["status"] == CONTRACT_VALID
        assert view["schema"] == CONTRACT["schema"]

    def test_contract_view_pending_while_open(self, tmp_path) -> None:
        ledger = RunLedger(InMemoryRunStore(), clock=_clock())
        record = ledger.begin(agent_id="a1", n=1, output_contract=CONTRACT)
        view = contract_view(record)
        assert view["status"] == CONTRACT_PENDING
        assert view["violations"] == []
        assert contract_view(ledger.begin(agent_id="a1", n=2)) is None  # no contract → no view

    def test_null_structured_output_roundtrips(self, tmp_path) -> None:
        """A valid ``null`` output must persist, not be mistaken for absent."""
        ledger = RunLedger(FileRunStore(tmp_path), clock=_clock())
        contract = {"schema": {"type": "null"}, "enforcement": "strict"}
        ledger.begin(agent_id="a1", n=1, output_contract=contract)
        status, error, structured, meta = apply_output_contract("FINISHED", None, "null", contract)
        assert status == "FINISHED" and meta["status"] == CONTRACT_VALID
        assert structured is None
        ledger.finish(
            "a1",
            1,
            status=status,
            result_text="null",
            structured_output=structured,
            contract_result=meta,
        )
        raw = json.loads((tmp_path / "a1" / "run-1.json").read_text())
        assert "structured_output" in raw and raw["structured_output"] is None
        assert raw["contract_result"]["status"] == CONTRACT_VALID

    def test_terminal_monotonicity_survives_contract(self, tmp_path) -> None:
        """A late contract verdict never rewrites a persisted terminal run."""
        ledger = RunLedger(FileRunStore(tmp_path), clock=_clock())
        ledger.begin(agent_id="a1", n=1, output_contract=CONTRACT)
        ledger.finish(
            "a1",
            1,
            status="ERROR",
            error={"code": CODE_CONTRACT_VIOLATION},
            contract_result={"status": CONTRACT_INVALID},
        )
        again = ledger.finish(
            "a1",
            1,
            status="FINISHED",
            result_text=VALID_JSON,
            structured_output={"ok": True},
            contract_result={"status": CONTRACT_VALID},
        )
        assert again.status == "ERROR"
        assert again.error["code"] == CODE_CONTRACT_VIOLATION

    def test_pathological_output_fails_closed_not_crashes(self) -> None:
        """A message with pathological nesting yields ERROR + violation —
        the enforcement seam must never raise on sandbox-written text."""
        deep = "[" * 5000 + "]" * 5000
        status, error, structured, meta = apply_output_contract("FINISHED", None, deep, CONTRACT)
        assert status == "ERROR"
        assert error["code"] == CODE_CONTRACT_VIOLATION
        assert meta["status"] == CONTRACT_INVALID
        # Parsed-then-depth-checked → max_depth; a scanner that trips at
        # parse time → not_json. Both are diagnosable invalids.
        assert meta["violations"][0]["code"] in ("max_depth", "not_json")

    def test_tampered_contract_fails_closed(self) -> None:
        """A ledger record whose schema is missing/non-object can never
        become a trivially-valid contract — strict flips to ERROR."""
        for contract in (
            {"enforcement": "strict", "schema_digest": "sha256:x"},
            {"enforcement": "strict", "schema": "not a dict"},
            {"enforcement": "strict", "schema": {}},
        ):
            status, error, _, meta = apply_output_contract("FINISHED", None, VALID_JSON, contract)
            assert status == "ERROR"
            assert error["code"] == CODE_CONTRACT_VIOLATION
            assert meta["status"] == CONTRACT_INVALID
            assert meta["violations"][0]["code"] == "evaluation_error"

    def test_terminal_record_without_verdict_reports_skipped(self) -> None:
        """A cancelled contracted run must not read ``pending`` forever."""
        ledger = RunLedger(InMemoryRunStore(), clock=_clock())
        ledger.begin(agent_id="a1", n=1, output_contract=CONTRACT)
        cancelled = ledger.cancel("a1", 1)
        assert contract_view(cancelled)["status"] == CONTRACT_SKIPPED
        got = ledger.get("a1", 1)
        assert contract_view(got)["status"] == CONTRACT_SKIPPED

    def test_outcome_corrupt_usage_degrades_not_crashes(self) -> None:
        """Corrupt usage fields are dropped — evidence problems must never
        wedge the terminal persist."""
        status, error, _, usage = outcome_from_turn_payload(
            {"status": "success", "message": "ok", "usage": {"input_tokens": "abc"}}
        )
        assert status == "FINISHED"
        assert usage == {}
        status, _, _, _ = outcome_from_turn_payload(["not", "a", "dict"])
        assert status == "ERROR"

    def test_record_from_dict_roundtrips_contract_fields(self) -> None:
        raw = {
            "agent_id": "a1",
            "n": 1,
            "status": "FINISHED",
            "output_contract": CONTRACT,
            "structured_output": {"ok": True},
            "contract_result": {"status": CONTRACT_VALID},
        }
        record = record_from_dict(raw)
        assert record.output_contract == CONTRACT
        assert record.structured_output == {"ok": True}
        assert record.contract_result["status"] == CONTRACT_VALID
        for bad_key in ("output_contract", "contract_result"):
            bad = {**raw, bad_key: "nope"}
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
