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


def enqueue_quarantine_releases(db: Any) -> int:
    """Keep resolving quarantined compute: re-enqueue release until stop is confirmed.

    ``enqueue_job`` dedupes on (kind, target), so this is safe to run periodically even
    while a release Job for the lease is still queued or retrying.
    """

    def fn(uow: Any) -> int:
        leases = uow.find("executor_leases", {"quarantined": True})
        for lease in leases:
            uow.enqueue_job(
                workspace_id=lease["workspace_id"],
                kind="executor.release",
                target_id=lease["id"],
                session_id=lease["session_id"],
                input={"reason": "quarantine"},
            )
        return len(leases)

    return db.run(fn)
