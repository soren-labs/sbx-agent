"""Harness honesty and manifest data drift (A12)."""

from __future__ import annotations

import json
from pathlib import Path

from protocol.capabilities import CAPABILITY_NAMES
from runtime.harnesses.opencode import OpenCodeHarness
from runtime.harnesses.registry import manifest_data

ROOT = Path(__file__).resolve().parents[3]


def test_published_manifests_match_registry() -> None:
    published = json.loads((ROOT / "docs/specs/unified/harnesses/manifests.json").read_text())
    assert published == json.loads(json.dumps(manifest_data()))


def test_every_manifest_declares_all_capabilities_truthfully() -> None:
    for manifest in manifest_data():
        assert set(manifest["capabilities"]) == set(CAPABILITY_NAMES), manifest["provider_id"]
        for cap in manifest["capabilities"].values():
            assert cap["status"] in ("supported", "unsupported", "unknown")
    tiers = {m["provider_id"]: m["support_tier"] for m in manifest_data()}
    assert tiers["opencode"] == "supported"
    assert all(tiers[p] == "disabled" for p in ("claude", "devin", "grok", "antigravity"))


def test_opencode_normalizes_recorded_stream_and_never_invents_usage() -> None:
    fixture = ROOT / "tests/fixtures/harnesses/opencode/success.jsonl"
    harness = OpenCodeHarness("1.18.29")
    from runtime.harnesses.protocol import TurnContext

    ctx = TurnContext("s", "t", "e", "o", 1, Path("/w"), Path("/h"), "p")
    state = harness.new_state(ctx)
    out = [o for line in fixture.read_text().splitlines() for o in harness.normalize(line, state)]
    kinds = [o["type"] for o in out]
    assert kinds[0] == "diagnostic.reported" or kinds[0] == "execution.native_bound"
    assert "tool.completed" in kinds and "message.part_added" in kinds
    assert harness.finish(state)[0]["payload"]["source"] == "opencode.step_finish"
    empty = harness.new_state(ctx)
    harness.normalize('{"type":"text","sessionID":"s1","part":{"id":"p","text":"hi"}}', empty)
    assert harness.finish(empty) == [], "missing usage is absent, not synthetic zero"
    assert harness.classify_outcome({"exit_code": None}, empty).verdict == "unknown"
    weird = harness.normalize('{"type":"future_kind","sessionID":"s1"}', harness.new_state(ctx))
    assert all(o["type"] != "diagnostic.reported" for o in weird), "unknown valid frames tolerated"
