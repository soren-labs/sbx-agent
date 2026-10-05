"""Handler registry — JobKind -> handler callable.

Phase 2 registers the runtime plane: turn.dispatch drives admission onto
an ExecutorLease; executor.reconcile sweeps expired/lost leases and fails
their nonterminal executions. An unregistered kind still fails terminally
with `unimplemented` — jobs are never silently dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime

from control.jobs.worker import JobContext

# Wired at composition (set_runtime_stack); handlers resolve lazily so the
# registry stays importable without a live runtime plane.
_RUNTIME_STACK = None


def set_runtime_stack(stack) -> None:
    global _RUNTIME_STACK
    _RUNTIME_STACK = stack


def turn_dispatch(job: dict, ctx: JobContext) -> dict:
    if _RUNTIME_STACK is None:
        raise RuntimeError("runtime plane not wired — cannot dispatch turn")
    payload = job.get("payload") or {}
    turn_id = payload.get("turn_id") or job.get("target_id")
    return _RUNTIME_STACK.service.dispatch_turn(
        ctx.uow, workspace_id=job["workspace_id"], turn_id=turn_id
    )


def executor_reconcile(job: dict, ctx: JobContext) -> dict:
    if _RUNTIME_STACK is None:
        return {"skipped": "no runtime plane"}
    lost = _RUNTIME_STACK.service.reconcile_expired_leases(ctx.uow, datetime.now(UTC))
    return {"lost_leases": lost}


def retention_cleanup(job: dict, ctx: JobContext) -> dict:
    """Sweep time-expired durable state: pending waits past deadline,
    held capacity past expiry, expired refresh claims. Expired leases get
    the full lost-transition (executions/turns quarantined) when a runtime
    plane is wired; otherwise they are marked lost here and reconciled by
    executor.reconcile."""
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
    lost = 0
    if _RUNTIME_STACK is not None:
        lost = _RUNTIME_STACK.service.reconcile_expired_leases(ctx.uow, datetime.now(UTC))
    else:
        leases = conn.execute(
            "UPDATE executor_leases SET state='lost', updated_at=now()"
            " WHERE state IN ('allocating','ready','quiescing')"
            " AND expires_at IS NOT NULL AND expires_at < now() RETURNING id"
        ).fetchall()
        lost = len(leases)
    return {
        "expired_waits": len(waits),
        "expired_reservations": len(caps),
        "lost_leases": lost,
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
    "turn.dispatch": turn_dispatch,
    "executor.reconcile": executor_reconcile,
    "execution.reconcile": executor_reconcile,
}
