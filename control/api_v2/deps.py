"""Dependency providers for ``/v2`` (SOR-256).

The V2 surface reuses the V1 dependency providers for every shared resource
(plane, stores, scheduler, auth) — this module only owns the V2-specific
``SessionEventsHub`` (one per app) plus the ACK budget knob.
"""

from __future__ import annotations

import os

from fastapi import Request

from control.api_v1.deps import get_plane, get_run_states, get_task_store, get_v1_state
from control.api_v2.events import SessionEventsHub

_DEFAULT_POLL_S = float(os.environ.get("SBX_V2_EVENTS_POLL_S", "0.5"))
_DEFAULT_ACK_BUDGET_S = float(os.environ.get("SBX_V2_ACK_BUDGET_S", "0.75"))


def get_v2_hub(request: Request) -> SessionEventsHub:
    """The app's shared events hub — lazily bound to the live stores."""
    hub = getattr(request.app.state, "v2_events", None)
    if hub is None:
        hub = SessionEventsHub(
            plane=get_plane(request),
            task_store=get_task_store(request),
            run_states=get_run_states(request),
            revisions=getattr(request.app.state, "revisions", None),
            poll_s=float(getattr(request.app.state, "v2_events_poll_s", _DEFAULT_POLL_S)),
        )
        request.app.state.v2_events = hub
    return hub


def get_ack_budget(request: Request) -> float:
    return float(getattr(request.app.state, "v2_ack_budget_s", _DEFAULT_ACK_BUDGET_S))


__all__ = [
    "get_v2_hub",
    "get_ack_budget",
    "get_plane",
    "get_run_states",
    "get_task_store",
    "get_v1_state",
]
