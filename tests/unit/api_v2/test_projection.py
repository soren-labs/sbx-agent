"""SOR-256: status/phase + projection mapping unit tests (no server)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from control.api_v2 import projection as proj
from control.api_v2.events import normalize_raw
from control.api_v2.schemas import (
    DELIVERY_MODES,
    EVENT_TYPES,
    SESSION_PHASES,
    SESSION_STATUSES,
    CreateSessionRequest,
    SessionDetailView,
    SessionStatusEvent,
)
from pydantic import ValidationError


def _record(**over: Any) -> Any:
    base = dict(
        id="task_x",
        owner="key",
        status="queued",
        request={"prompt": {"text": "p"}},
        resolved=None,
        agent_id=None,
        run_id=None,
        created_at="t0",
        updated_at="t0",
        response=None,
        idempotency=None,
        transitions=[],
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_status_map_covers_every_aggregate() -> None:
    for aggregate in (
        "queued",
        "running",
        "delivering",
        "finished",
        "error",
        "expired",
        "delivery_failed",
        "cancelled",
        "failed",
    ):
        assert proj.public_status(aggregate) in SESSION_STATUSES


def test_status_map_values() -> None:
    assert proj.public_status("queued") == "queued"
    assert proj.public_status("running") == "running"
    assert proj.public_status("delivering") == "running"
    assert proj.public_status("finished") == "finished"
    for failed in ("error", "expired", "delivery_failed", "failed"):
        assert proj.public_status(failed) == "failed"
    assert proj.public_status("cancelled") == "cancelled"


def test_phase_map() -> None:
    queued = _record(agent_id=None)
    assert proj.public_phase(queued, None, [], "queued") == "resolving"
    bound = _record(agent_id="agent-1")
    creating = SimpleNamespace(status="creating")
    assert proj.public_phase(bound, creating, [], "queued") == "provisioning"
    idle = SimpleNamespace(status="idle")
    assert proj.public_phase(bound, idle, [(1, "CREATING")], "queued") == "starting_provider"
    assert proj.public_phase(bound, idle, [(1, "QUEUED")], "queued") == "queued"
    assert proj.public_phase(bound, idle, [(1, "RUNNING")], "running") == "running"
    assert proj.public_phase(bound, idle, [(1, "FINISHED")], "delivering") == "publishing"
    for terminal in ("finished", "failed", "cancelled"):
        assert proj.public_phase(bound, idle, [], terminal) == terminal


def test_changes_view_statuses() -> None:
    rev = SimpleNamespace(
        revision_id="rev-1",
        n=1,
        status="ready",
        base_sha="aaa",
        head_sha="bbb",
        created_at="t",
        error=None,
        repo="r",
        delivery=None,
    )
    assert proj.changes_view([rev], {"head_sha": "bbb", "base_sha": "aaa"})["status"] == "ready"
    assert proj.changes_view([rev], {"head_sha": "aaa", "base_sha": "aaa"})["status"] == "unchanged"
    assert proj.changes_view([], {"head_sha": None})["status"] == "none"
    failed_rev = SimpleNamespace(
        revision_id="rev-2",
        n=2,
        status="materialization_failed",
        base_sha="a",
        head_sha=None,
        created_at="t",
        error={"message": "boom"},
        repo="r",
        delivery=None,
    )
    view = proj.changes_view([rev, failed_rev], {"head_sha": "ccc", "base_sha": "aaa"})
    assert view["status"] == "materialization_failed"
    assert view["count"] == 2
    assert view["revisions"][1]["error"] == "boom"


def test_delivery_view_pending_delivered_failed() -> None:
    record = _record()
    # No delivery policy on ws or resolved spec → delivery is not applicable.
    assert proj.delivery_view(record, None, []) is None
    ws_pending = {"git": {"push": True}}
    out = proj.delivery_view(record, ws_pending, [])
    assert out["status"] == "pending"
    ws_done = {
        "git": {"push": True, "auto_create_pr": True},
        "branch": "feat/x",
        "pushed_head_sha": "deadbeef",
        "pull_request": {"url": "https://x/pr/1", "number": 1, "state": "open"},
    }
    out = proj.delivery_view(record, ws_done, [])
    assert out["status"] == "delivered"
    assert out["branch"] == "feat/x"
    assert out["pull_request"]["number"] == 1
    ws_err = {"git": {"push": True}, "publish_error": "push rejected"}
    out = proj.delivery_view(record, ws_err, [])
    assert out["status"] == "failed"
    assert "push rejected" in out["error"]


def test_strict_request_schema_rejects_extras() -> None:
    with pytest.raises(ValidationError):
        CreateSessionRequest.model_validate({"prompt": "x", "task_id": "leak"})


def test_response_schemas_validate() -> None:
    detail = SessionDetailView(
        id="task_1",
        status="finished",
        phase="finished",
        prompt="p",
        messages=[{"id": "msg-1", "role": "user", "text": "p"}],
        activities=[{"id": "a", "kind": "command_execution", "status": "completed"}],
        changes={"status": "ready", "count": 1, "revisions": []},
        delivery={"status": "delivered", "pull_request": {"url": "u"}},
        created_at="t",
        updated_at="t",
    )
    assert detail.delivery.pull_request.url == "u"
    event = SessionStatusEvent(status="running", phase="running")
    assert event.status == "running"


def test_event_vocabulary_frozen() -> None:
    assert set(EVENT_TYPES) == {
        "session.status",
        "message.created",
        "activity.started",
        "activity.updated",
        "activity.completed",
        "usage.updated",
        "changes.updated",
        "delivery.updated",
        "session.completed",
        "session.failed",
    }
    assert set(SESSION_PHASES) == {
        "resolving",
        "queued",
        "provisioning",
        "starting_provider",
        "running",
        "publishing",
        "finished",
        "failed",
        "cancelled",
    }
    assert set(DELIVERY_MODES) == {"none", "branch", "pull_request", "auto"}


def test_normalize_raw_canonical_events() -> None:
    item = {"id": "i1", "type": "command_execution", "command": "ls"}
    out = normalize_raw({"type": "item.started", "item": item})
    assert out[0][0] == "activity.started"
    activity = out[0][1]["activity"]
    assert activity["id"] == "i1"
    assert activity["kind"] == "command_execution"
    assert activity["status"] == "running"
    assert activity["command"] == "ls"
    done = normalize_raw(
        {
            "type": "item.completed",
            "item": {
                "id": "i1",
                "type": "file_change",
                "status": "completed",
                "changes": [{"path": "f.txt"}],
            },
        }
    )
    assert done[0][0] == "activity.completed"
    assert done[0][1]["activity"]["path"] == "f.txt"
    usage = normalize_raw(
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}, "n": 1}
    )
    assert usage == [
        ("usage.updated", {"usage": {"input_tokens": 1, "output_tokens": 2}, "run": 1})
    ]
    err = normalize_raw({"type": "error", "message": "oops"})
    assert err[0][0] == "activity.completed" and err[0][1]["activity"]["kind"] == "error"
    # Engine bookkeeping folds into the state-driven events, not the stream.
    for noise in (
        "thread.started",
        "turn.started",
        "turn.failed",
        "sbx.turn_started",
        "sbx.turn_finished",
        "sbx.error",
        "sbx.session_meta",
    ):
        assert normalize_raw({"type": noise}) == []
