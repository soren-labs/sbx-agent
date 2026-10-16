"""Conversation-total usage becomes per-Turn usage (issue #228 LEGACY205-USAGE).

The numbers are the sanitized Antigravity samples recorded for #205
(docs/reports/issue-205/live/live-antigravity.json): the first Turn reported 35,996 input /
249 output tokens; the resumed Turn, which only recalled one word, reported 48,492 / 251.
"""

from __future__ import annotations

from typing import Any

from control.application.ingest import _turn_usage
from control.domain.text import storable


class Uow:
    def __init__(self, rows: dict[str, dict[str, dict[str, Any]]]) -> None:
        self.rows = rows

    def get(self, table: str, id: str) -> dict[str, Any] | None:
        return self.rows.get(table, {}).get(id)

    def find(self, table: str, where: dict[str, Any], *, order: str = "", limit: int = 0):
        items = [
            r for r in self.rows.get(table, {}).values() if r["session_id"] == where["session_id"]
        ]
        return sorted(items, key=lambda r: r["ordinal"], reverse=True)[: limit or None]


def reported(input_tokens: int, output_tokens: int) -> dict[str, Any]:
    return {
        "source": "antigravity.result",
        "complete": True,
        "scope": "conversation",
        "input_tokens": input_tokens,
        "cached_input_tokens": 0,
        "output_tokens": output_tokens,
        "reasoning_tokens": 0,
    }


def setup(*, first_usage: dict[str, Any] | None) -> tuple[Uow, dict[str, Any]]:
    binding = {"id": "nb1", "native_id": "e51e01a6", "execution_id": "x1"}
    rows: dict[str, dict[str, dict[str, Any]]] = {
        "native_context_bindings": {"nb1": binding},
        "executions": {
            "x1": {"id": "x1", "native_binding_id": "nb1"},
            "x2": {"id": "x2", "native_binding_id": "nb1"},  # resumed the same conversation
        },
        "turns": {
            "t1": {"id": "t1", "session_id": "s1", "ordinal": 1, "usage": first_usage},
            "t2": {"id": "t2", "session_id": "s1", "ordinal": 2, "usage": None},
        },
    }
    return Uow(rows), rows


def test_first_turn_of_a_conversation_keeps_its_total() -> None:
    uow, rows = setup(first_usage=None)
    usage = _turn_usage(uow, rows["executions"]["x1"], rows["turns"]["t1"], reported(35996, 249))
    assert usage["per_turn"] is True
    assert (usage["input_tokens"], usage["output_tokens"]) == (35996, 249)
    assert usage["conversation_total"]["native_id"] == "e51e01a6"


def test_resumed_turn_reports_only_its_own_share() -> None:
    uow, rows = setup(first_usage=None)
    rows["turns"]["t1"]["usage"] = _turn_usage(
        uow, rows["executions"]["x1"], rows["turns"]["t1"], reported(35996, 249)
    )
    usage = _turn_usage(uow, rows["executions"]["x2"], rows["turns"]["t2"], reported(48492, 251))
    assert usage["per_turn"] is True
    assert (usage["input_tokens"], usage["output_tokens"]) == (12496, 2)
    assert usage["conversation_total"]["input_tokens"] == 48492
    # Summing the Turns now equals the conversation total instead of double counting it.
    total = rows["turns"]["t1"]["usage"]["input_tokens"] + usage["input_tokens"]
    assert total == 48492


def test_unknown_baseline_is_marked_rather_than_guessed() -> None:
    # The first Turn was cancelled before reporting usage: the resumed total cannot be split.
    uow, rows = setup(first_usage=None)
    usage = _turn_usage(uow, rows["executions"]["x2"], rows["turns"]["t2"], reported(48492, 251))
    assert usage["per_turn"] is False and usage["input_tokens"] == 48492


def test_other_harnesses_are_unchanged() -> None:
    uow, rows = setup(first_usage=None)
    payload = {"source": "codex", "input_tokens": 10, "output_tokens": 2}
    assert _turn_usage(uow, rows["executions"]["x1"], rows["turns"]["t1"], payload) == payload


def test_storable_text_is_deterministic() -> None:
    value = {"a\x00": ["x\ud800", 1, None], "ok": "fine"}
    once, n = storable(value)
    assert n == 2 and once == {"a�": ["x�", 1, None], "ok": "fine"}
    assert storable(once) == (once, 0)
    assert storable(value)[0] == once
