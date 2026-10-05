"""Handler registry — JobKind -> handler callable.

Phase 1 ships the handlers whose effects are fully defined by the
foundation schema; later phases register the rest against the same worker.
An unregistered kind fails terminally with `unimplemented` — jobs are never
silently dropped.
"""

from __future__ import annotations

from control.jobs.worker import JobContext


def retention_cleanup(job: dict, ctx: JobContext) -> dict:
    """Sweep time-expired durable state: pending waits past deadline,
    held capacity past expiry, expired refresh claims."""
    conn = ctx.uow.conn
    waits = conn.execute(
        "UPDATE wait_subscriptions SET state='expired', updated_at=now()"
        " WHERE state='pending' AND deadline_at IS NOT NULL"
        " AND deadline_at < now() RETURNING id"
    ).fetchall()
    caps = conn.execute(
        "UPDATE capacity_reservations SET state='expired'"
        " WHERE state='held' AND expires_at IS NOT NULL"
        " AND expires_at < now() RETURNING id"
    ).fetchall()
    leases = conn.execute(
        "UPDATE executor_leases SET state='lost', updated_at=now()"
        " WHERE state IN ('allocating','ready','quiescing')"
        " AND expires_at IS NOT NULL AND expires_at < now() RETURNING id"
    ).fetchall()
    return {
        "expired_waits": len(waits),
        "expired_reservations": len(caps),
        "lost_leases": len(leases),
    }


def outbox_deliver(job: dict, ctx: JobContext) -> dict:
    """Drain this workspace's due outbox rows (local delivery)."""
    rows = ctx.uow.outbox.due(limit=100)
    for row in rows:
        ctx.uow.outbox.mark(row["workspace_id"], row["id"], "delivered")
    return {"delivered": len(rows)}


HANDLERS = {
    "retention.cleanup": retention_cleanup,
    "outbox.deliver": outbox_deliver,
}
