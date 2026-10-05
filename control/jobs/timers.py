"""Timers only enqueue deduped Jobs; they never mutate Session/lease state directly."""

from __future__ import annotations

from datetime import timedelta
from typing import Any


def enqueue_idle_releases(db: Any, *, idle_seconds: float) -> int:
    def fn(uow: Any) -> int:
        count = 0
        cutoff = uow.now() - timedelta(seconds=idle_seconds)
        for lease in uow.find("executor_leases", {"state": "ready"}):
            session = uow.get("sessions", lease["session_id"])
            busy = uow.count(
                "turns",
                {
                    "session_id": session["id"],
                    "state": ["queued", "preparing", "running", "cancelling"],
                },
            )
            if busy or session["updated_at"] > cutoff:
                continue
            uow.enqueue_job(
                workspace_id=lease["workspace_id"],
                kind="executor.release",
                target_id=lease["id"],
                session_id=session["id"],
                input={"reason": "idle"},
            )
            count += 1
        return count

    return db.run(fn)
