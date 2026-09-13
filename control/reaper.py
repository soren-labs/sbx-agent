"""Idle / orphan reaper as a pure function (clock injected via ``now``)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from control.backend import SandboxBackend
from control.config import IDLE_TIMEOUT_S, TERMINAL_STATUSES
from control.store import SessionStore


@dataclass(frozen=True)
class ReapAction:
    kind: str
    session_id: str | None
    sandbox_id: str | None


def reap(
    store: SessionStore,
    backend: SandboxBackend,
    now: datetime,
    *,
    idle_timeout_s: int = IDLE_TIMEOUT_S,
) -> list[ReapAction]:
    """Reconcile Dict records with live sandboxes.

    Rules (SOR-31 + P0):
    * idle longer than ``idle_timeout_s`` and sandbox still alive → terminate + ``timed_out``
    * record exists, sandbox gone, status was idle → ``timed_out`` (native idle_timeout)
    * record exists, sandbox gone, status was creating/running → ``lost``
    * sandbox exists with no Dict record → terminate
    """
    actions: list[ReapAction] = []
    known_ids: set[str] = set()

    for rec in store.list_all():
        if rec.sandbox_id:
            known_ids.add(rec.sandbox_id)
        if rec.status in TERMINAL_STATUSES:
            continue
        handle = rec.handle()
        poll = backend.poll(handle) if handle is not None else None
        alive = bool(poll and poll.alive)
        last = rec.last_activity_at or rec.updated_at
        idle_expired = (now - last).total_seconds() >= idle_timeout_s

        if not alive:
            rec.status = "timed_out" if rec.status == "idle" else "lost"
            rec.ended_at = now
            rec.updated_at = now
            rec.current_turn_id = None
            rec.current_turn_n = None
            store.put(rec)
            actions.append(ReapAction(rec.status, rec.id, rec.sandbox_id))
            continue

        if rec.status == "idle" and idle_expired and handle is not None:
            backend.terminate(handle)
            rec.status = "timed_out"
            rec.ended_at = now
            rec.updated_at = now
            rec.current_turn_id = None
            rec.current_turn_n = None
            store.put(rec)
            actions.append(ReapAction("timed_out", rec.id, rec.sandbox_id))

    for handle in backend.list():
        if handle.id in known_ids:
            continue
        backend.terminate(handle)
        actions.append(ReapAction("orphan_terminate", None, handle.id))

    return actions
