from __future__ import annotations

import json

from control.run_activity import (
    FileRunActivityStore,
    InMemoryRunActivityStore,
    compact_run_events,
)


def _log(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _cmd(event_type: str, item_id: str, output: str = "") -> dict:
    return {
        "type": event_type,
        "item": {
            "id": item_id,
            "type": "command_execution",
            "command": "ls",
            "aggregated_output": output,
        },
    }


LOG = _log(
    {"type": "sbx.session_meta", "provider": "codex"},
    {"type": "sbx.turn_started", "n": 1},
    _cmd("item.started", "a"),
    _cmd("item.completed", "a", "x"),
    {"type": "sbx.turn_finished", "status": "success"},
    {"type": "sbx.turn_started", "n": 2},
    _cmd("item.started", "b"),
    "not json",
    {"type": "sbx.turn_finished", "status": "success"},
)


def test_slices_run_and_keeps_line_numbers() -> None:
    run1 = compact_run_events(LOG, 1)
    assert [e["id"] for e in run1] == [1, 2, 4, 5]
    assert run1[2]["event"]["type"] == "item.completed"


def test_in_progress_item_without_completion_is_kept() -> None:
    run2 = compact_run_events(LOG, 2)
    assert [e["event"]["type"] for e in run2] == [
        "sbx.turn_started",
        "item.started",
        "sbx.turn_finished",
    ]


def test_long_output_is_trimmed_and_transcript_bounded() -> None:
    big = _log(
        {"type": "sbx.turn_started", "n": 1},
        *[_cmd("item.completed", str(i), "y" * 50_000) for i in range(20)],
    )
    entries = compact_run_events(big, 1, max_bytes=64 * 1024)
    assert len(json.dumps(entries)) <= 64 * 1024 + 512
    outputs = [e["event"]["item"]["aggregated_output"] for e in entries if "item" in e["event"]]
    assert all("characters trimmed" in o for o in outputs)
    assert entries[-1]["id"] == 21


def test_over_budget_drops_oldest_with_marker() -> None:
    big = _log(
        {"type": "sbx.turn_started", "n": 1},
        *[_cmd("item.completed", str(i), "z" * 5_000) for i in range(40)],
    )
    entries = compact_run_events(big, 1, max_bytes=16 * 1024)
    assert entries[0]["event"]["type"] == "sbx.error"
    assert "omitted" in entries[0]["event"]["message"]
    assert entries[-1]["id"] == 41


def test_empty_log() -> None:
    assert compact_run_events(None, 1) == []
    assert compact_run_events("", 1) == []


def test_stores_round_trip(tmp_path) -> None:
    entries = compact_run_events(LOG, 1)
    for store in (InMemoryRunActivityStore(), FileRunActivityStore(tmp_path)):
        assert store.get("agent", 1) is None
        store.put("agent", 1, entries)
        assert store.get("agent", 1) == entries
    (tmp_path / "agent" / "run-2.json").write_text("{broken")
    assert FileRunActivityStore(tmp_path).get("agent", 2) is None
