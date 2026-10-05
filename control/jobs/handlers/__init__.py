"""Handler registry — JobKind -> handler callable.

Phase 2 registers the runtime plane: turn.dispatch drives admission onto
an ExecutorLease; executor.reconcile sweeps expired/lost leases and fails
their nonterminal executions. An unregistered kind still fails terminally
with `unimplemented` — jobs are never silently dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime

from control.jobs.worker import JobContext

# Wired at composition (set_runtime_stack / set_connection_plane); handlers
# resolve lazily so the registry stays importable without a live plane.
_RUNTIME_STACK = None
_CONNECTION_SERVICE = None
_CONNECTOR_REGISTRY = None
_CHANGE_SERVICE = None
_DELIVERY_SERVICE = None
_DELEGATION_SERVICE = None


def set_runtime_stack(stack) -> None:
    global _RUNTIME_STACK
    _RUNTIME_STACK = stack


def set_connection_plane(connection_service, connector_registry) -> None:
    global _CONNECTION_SERVICE, _CONNECTOR_REGISTRY
    _CONNECTION_SERVICE = connection_service
    _CONNECTOR_REGISTRY = connector_registry


def set_change_plane(change_service, delegation_service=None) -> None:
    global _CHANGE_SERVICE, _DELEGATION_SERVICE
    _CHANGE_SERVICE = change_service
    if delegation_service is not None:
        _DELEGATION_SERVICE = delegation_service


def set_delivery_plane(delivery_service) -> None:
    global _DELIVERY_SERVICE
    _DELIVERY_SERVICE = delivery_service


def set_delegation_plane(delegation_service) -> None:
    global _DELEGATION_SERVICE
    _DELEGATION_SERVICE = delegation_service


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


def connection_validate(job: dict, ctx: JobContext) -> dict:
    """Minimal documented probe of the selected credential version; only
    safe identity/capability metadata is written back (never plaintext)."""
    if _CONNECTION_SERVICE is None or _CONNECTOR_REGISTRY is None:
        return {"skipped": "connection plane not wired"}
    payload = job.get("payload") or {}
    connection_id = payload.get("connection_id") or job.get("target_id")
    cred_id = payload.get("credential_version_id")
    uow = ctx.uow
    conn = uow.connections.get(job["workspace_id"], connection_id)
    if conn is None:
        return {"skipped": "connection gone"}
    # Validation is bound to the version that was current when the job was
    # enqueued — a replacement supersedes it.
    if conn["current_credential_version_id"] != cred_id:
        return {"skipped": "superseded by newer credential version"}

    from control.application.connections import PURPOSE_RUNTIME_EXECUTION

    try:
        material = _CONNECTION_SERVICE.materialize(
            uow,
            workspace_id=job["workspace_id"],
            connection_id=connection_id,
            purpose=(
                PURPOSE_RUNTIME_EXECUTION
                if conn["kind"] in ("opencode_zen", "codex")
                else ("executor_worker" if conn["kind"] == "modal" else "delivery_worker")
            ),
        )
    except Exception as exc:  # revoked/invalid between enqueue and run
        uow.connections.update(
            job["workspace_id"],
            connection_id,
            {"health": "degraded"},
        )
        return {"ok": False, "reason": getattr(exc, "code", "materialize_failed")}

    try:
        connector = _CONNECTOR_REGISTRY.get(conn["kind"])
        result = connector.validate(material.format, material.payload)
    except Exception as exc:
        result = None
        uow.connections.update(
            job["workspace_id"],
            connection_id,
            {"health": "degraded"},
        )
        return {"ok": False, "reason": f"probe_error:{type(exc).__name__}"}
    finally:
        del material

    health = (
        "ready"
        if (result and result.ok)
        else ("reauth_required" if result and result.reason == "auth_failed" else "degraded")
    )
    uow.connections.update(
        job["workspace_id"],
        connection_id,
        {
            "health": health,
            "external_identity": result.external_identity if result else {},
            "capability_observations": result.capabilities if result else {},
        },
    )
    _CONNECTION_SERVICE.record_observation(
        uow,
        workspace_id=job["workspace_id"],
        connection_id=connection_id,
        credential_version_id=cred_id,
        kind="validation",
        scope=conn["kind"],
        result={
            "ok": bool(result and result.ok),
            "reason": result.reason if result else "probe_error",
            "capabilities": result.capabilities if result else {},
        },
        ttl_s=3600,
    )
    return {"ok": bool(result and result.ok), "health": health}


def changeset_capture(job: dict, ctx: JobContext) -> dict:
    if _CHANGE_SERVICE is None:
        return {"skipped": "change plane not wired"}
    return _CHANGE_SERVICE.perform_capture(job, ctx)


def changeset_apply(job: dict, ctx: JobContext) -> dict:
    if _CHANGE_SERVICE is None:
        return {"skipped": "change plane not wired"}
    return _CHANGE_SERVICE.perform_apply(job, ctx)


def delivery_perform(job: dict, ctx: JobContext) -> dict:
    if _DELIVERY_SERVICE is None:
        return {"skipped": "delivery plane not wired"}
    return _DELIVERY_SERVICE.perform_delivery(job, ctx)


def delivery_merge(job: dict, ctx: JobContext) -> dict:
    if _DELIVERY_SERVICE is None:
        return {"skipped": "delivery plane not wired"}
    return _DELIVERY_SERVICE.perform_merge(job, ctx)


def delegation_publish_result(job: dict, ctx: JobContext) -> dict:
    if _DELEGATION_SERVICE is None:
        return {"skipped": "delegation plane not wired"}
    return _DELEGATION_SERVICE.publish_result(job, ctx)


HANDLERS = {
    "retention.cleanup": retention_cleanup,
    "outbox.deliver": outbox_deliver,
    "turn.dispatch": turn_dispatch,
    "executor.reconcile": executor_reconcile,
    "execution.reconcile": executor_reconcile,
    "connection.validate": connection_validate,
    "connection.provision": connection_validate,
    "credential.refresh": connection_validate,
    "changeset.capture": changeset_capture,
    "changeset.apply": changeset_apply,
    "delivery.perform": delivery_perform,
    "delivery.merge": delivery_merge,
    "delegation.publish_result": delegation_publish_result,
}
