"""Versioned offline reducer: rebuild current Session state from the journal.

Used to verify typed projections against committed history (RFC 04 rebuild,
A03). It never serves admission/merge/cancel decisions.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

REDUCER_VERSION = 1

_TURN_TERMINAL = {
    "turn.succeeded": "succeeded",
    "turn.failed": "failed",
    "turn.cancelled": "cancelled",
    "turn.interrupted": "interrupted",
}
_LIFECYCLE = {
    "session.created": "open",
    "session.archived": "archived",
    "session.unarchived": "open",
    "session.closed": "closed",
}


def replay_session(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    state: dict[str, Any] = {
        "reducer_version": REDUCER_VERSION,
        "lifecycle": None,
        "turns": {},
        "messages": 0,
        "last_seq": 0,
    }
    for event in events:
        if event["seq"] != state["last_seq"] + 1:
            raise ValueError(f"journal gap before seq {event['seq']}")
        state["last_seq"] = event["seq"]
        kind = event["type"]
        turn_id = event.get("turn_id")
        if kind in _LIFECYCLE:
            state["lifecycle"] = _LIFECYCLE[kind]
        elif kind == "message.accepted":
            state["messages"] += 1
        elif kind == "turn.queued":
            state["turns"][turn_id] = "queued"
        elif kind == "turn.preparing":
            state["turns"][turn_id] = "preparing"
        elif kind == "turn.started":
            state["turns"][turn_id] = "running"
        elif kind == "turn.cancel_requested" and state["turns"].get(turn_id) in (
            "preparing",
            "running",
        ):
            state["turns"][turn_id] = "cancelling"
        elif kind in _TURN_TERMINAL:
            state["turns"][turn_id] = _TURN_TERMINAL[kind]
    return state
