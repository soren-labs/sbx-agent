"""Control-plane unit tests for public session payload."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from control.backend import LocalProcessBackend, SandboxSpec
from control.service import ControlPlane, format_sse
from control.store import InMemoryStore


def test_public_cost_uses_sandbox_seconds() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    plane = ControlPlane(
        backend,
        store,
        [
            sys.executable,
            str(Path(__file__).resolve().parents[3] / "tests" / "fakes" / "stub_runner.py"),
        ],
        clock=lambda: datetime(2026, 9, 13, 12, 0, 30, tzinfo=UTC),
    )
    handle = backend.create(SandboxSpec(tags={"owner": "sbx"}))
    created = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    from control.store import SessionRecord, empty_usage

    rec = SessionRecord(
        id="s",
        title="t",
        status="idle",
        created_at=created,
        updated_at=created,
        model="gpt-5.6-luna",
        turns=0,
        usage=empty_usage(),
        messages=[],
        owner="sbx",
        sandbox_id=handle.id,
        sandbox_root=str(handle.root),
        last_activity_at=created,
    )
    public = plane.public(rec)
    assert public["sandbox_seconds"] == 30.0
    assert public["cost_estimate_usd"] > 0
    rec.status = "closed"
    rec.ended_at = created + timedelta(seconds=12)
    public = plane.public(rec)
    assert public["sandbox_seconds"] == 12.0
    backend.terminate(handle)


def test_format_sse_uses_line_number_and_type() -> None:
    frame = format_sse(3, {"type": "sbx.turn_started", "n": 1})
    assert frame.startswith("id: 3\n")
    assert "event: sbx.turn_started\n" in frame
    assert '"n": 1' in frame
    assert frame.endswith("\n\n")
