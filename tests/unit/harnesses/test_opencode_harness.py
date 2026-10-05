"""OpenCode Harness — fixture-verified normalization, honest capability
manifest, outcome classification, native resume contract (RFC 167 §03)."""

from __future__ import annotations

from pathlib import Path

import pytest
from protocol.capabilities import CapabilityState
from protocol.errors import WIRE_CONTEXT_MISMATCH
from protocol.events import ObservationKind
from protocol.manifests import NativeContextBinding
from runtime.harnesses.opencode import OpencodeHarness
from runtime.harnesses.protocol import (
    CredentialBundle,
    NormalizeState,
    OutcomeKind,
    ProcessEvidence,
    TurnContext,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "events" / "opencode"


def _run_fixture(harness: OpencodeHarness, name: str, expected_native_id=None):
    state = NormalizeState()
    if expected_native_id:
        state.data["expected_native_id"] = expected_native_id
    observations = []
    for line in (FIXTURES / name).read_text().splitlines():
        if not line.strip():
            continue
        observations.extend(harness.normalize(line, state))
    return observations


class TestNormalize:
    def test_success_fixture(self, tmp_path):
        h = OpencodeHarness(tmp_path)
        obs = _run_fixture(h, "success.jsonl")
        kinds = [o.kind for o in obs]
        assert ObservationKind.THREAD_STARTED in kinds
        assert ObservationKind.TURN_STARTED in kinds
        assert ObservationKind.ITEM_COMPLETED in kinds
        assert ObservationKind.TURN_COMPLETED in kinds
        final = next(o for o in obs if o.kind == ObservationKind.TURN_COMPLETED)
        assert final.usage is not None and final.usage.input_tokens == 14290

    def test_tool_items(self, tmp_path):
        h = OpencodeHarness(tmp_path)
        obs = _run_fixture(h, "success.jsonl")
        tools = [
            o.item
            for o in obs
            if o.kind in (ObservationKind.ITEM_STARTED, ObservationKind.ITEM_COMPLETED)
            and o.item
            and o.item.get("type") == "command_execution"
        ]
        assert tools and all("id" in t for t in tools)

    def test_stale_resume_is_context_mismatch(self, tmp_path):
        h = OpencodeHarness(tmp_path)
        obs = _run_fixture(h, "success.jsonl", expected_native_id="ses_DIFFERENT")
        failed = [o for o in obs if o.kind == ObservationKind.TURN_FAILED]
        assert failed and failed[0].error["code"] == WIRE_CONTEXT_MISMATCH


class TestClassifyOutcome:
    def _evidence(self, tmp_path, fixture="success.jsonl", **kw):
        h = OpencodeHarness(tmp_path)
        obs = _run_fixture(h, fixture)
        kw.setdefault("signal", None)
        kw.setdefault("duration_ms", 100)
        return h, ProcessEvidence(observations=tuple(obs), **kw)

    def test_success(self, tmp_path):
        h, ev = self._evidence(tmp_path, exit_code=0)
        outcome = h.classify_outcome(ev)
        assert outcome.outcome == OutcomeKind.SUCCESS
        assert outcome.native_binding and outcome.native_binding.native_id.startswith("ses_")

    def test_nonzero_is_failure(self, tmp_path):
        h, ev = self._evidence(tmp_path, "nonzero.jsonl", exit_code=1)
        outcome = h.classify_outcome(ev)
        assert outcome.outcome == OutcomeKind.FAILURE

    def test_cancel_wins(self, tmp_path):
        h, ev = self._evidence(tmp_path, "success.jsonl", cancel_requested=True, exit_code=-15)
        # Terminal completion beats cancel — a turn that completed is a success.
        outcome = h.classify_outcome(ev)
        assert outcome.outcome == OutcomeKind.SUCCESS

    def test_unknown_exit(self, tmp_path):
        h, ev = self._evidence(tmp_path, "hang.jsonl", exit_code=None)
        outcome = h.classify_outcome(ev)
        assert outcome.outcome in (
            OutcomeKind.UNKNOWN,
            OutcomeKind.FAILURE,
            OutcomeKind.INTERRUPTED,
        )


class TestContract:
    def test_describe_honest(self, tmp_path):
        h = OpencodeHarness(tmp_path)
        manifest = h.describe()
        assert manifest.provider_id == "opencode"
        by_name = {c.name: c.state for c in manifest.capabilities}
        # Verified-capable
        assert by_name["event_stream"] == CapabilityState.SUPPORTED
        assert by_name["native_resume"] == CapabilityState.SUPPORTED
        # Honest negatives — one-shot run cannot steer or take approvals.
        assert by_name["steer"] == CapabilityState.UNSUPPORTED
        assert by_name["interactive_approval"] == CapabilityState.UNSUPPORTED
        # Unknown entries are never enabled.
        assert CapabilityState.UNKNOWN in by_name.values()

    def test_resume_requires_binding(self, tmp_path):
        h = OpencodeHarness(tmp_path)
        ctx = TurnContext(
            session_id="sess_1",
            turn_id="turn_1",
            execution_id="exec_1",
            attempt_ordinal=2,
            effect_id="eff_1",
            lease_generation=1,
            worktree_root=tmp_path,
            worktree_generation=0,
            prompt="hi",
        )
        prepared = h.prepare(ctx, CredentialBundle())
        with pytest.raises(Exception):
            h.resume_turn(
                ctx,
                prepared,
                NativeContextBinding(provider_id="codex", native_id="x", lineage_id="x"),
            )

    def test_prepare_isolates_home_and_writes_credentials(self, tmp_path):
        h = OpencodeHarness(tmp_path)
        ctx = TurnContext(
            session_id="sess_iso",
            turn_id="t",
            execution_id="e",
            attempt_ordinal=1,
            effect_id="eff",
            lease_generation=1,
            worktree_root=tmp_path,
            worktree_generation=0,
            prompt="x",
        )
        prepared = h.prepare(
            ctx,
            CredentialBundle(
                files={".local/share/opencode/auth.json": '{"token":"REDACTED"}'},
                env={"EXTRA": "1"},
            ),
        )
        auth = prepared.home / ".local/share/opencode/auth.json"
        assert auth.is_file() and auth.read_text() == '{"token":"REDACTED"}'
        assert oct(auth.stat().st_mode & 0o777) == "0o600"
        assert prepared.env["HOME"] == str(prepared.home)
        assert prepared.env["EXTRA"] == "1"

    def test_release_scrubs_auth_only(self, tmp_path):
        h = OpencodeHarness(tmp_path)
        ctx = TurnContext(
            session_id="sess_r",
            turn_id="t",
            execution_id="e",
            attempt_ordinal=1,
            effect_id="eff",
            lease_generation=1,
            worktree_root=tmp_path,
            worktree_generation=0,
            prompt="x",
        )
        prepared = h.prepare(
            ctx,
            CredentialBundle(files={".local/share/opencode/auth.json": "{}"}),
        )
        (prepared.xdg_data_home / "opencode" / "storage").mkdir(parents=True, exist_ok=True)
        assert h.release(prepared) == []
        assert not (prepared.home / ".local/share/opencode/auth.json").exists()
        # Native state (non-secret) survives release for verified resume.
        assert (prepared.xdg_data_home / "opencode").exists()

    def test_static_credentials_never_writeback(self, tmp_path):
        h = OpencodeHarness(tmp_path)
        assert h.export_refreshed_credentials("cred_1") is None
