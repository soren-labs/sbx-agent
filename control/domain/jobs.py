"""Durable Job vocabulary (RFC 04 Durable Job and claim protocol)."""

from __future__ import annotations

from control.domain.lifecycle import Lifecycle, table

JOB = Lifecycle(
    "job",
    table(
        {
            "queued": ("claimed", "cancelled"),
            "retry_wait": ("claimed", "cancelled"),
            "claimed": ("claimed", "queued", "retry_wait", "succeeded", "failed", "cancelled"),
        }
    ),
    frozenset({"succeeded", "failed", "cancelled"}),
)

ACTIVE_JOB_STATES = frozenset({"queued", "claimed", "retry_wait"})

# kind -> required target family
JOB_KINDS: dict[str, str] = {
    "turn.dispatch": "turn",
    "execution.reconcile": "execution",
    "executor.allocate": "lease",
    "executor.reconcile": "lease",
    "executor.release": "lease",
    "environment.build": "project_version",
    "worktree.restore": "lease",
    "snapshot.capture": "snapshot",
    "changeset.capture": "changeset",
    "changeset.apply": "worktree_operation",
    "delivery.perform": "delivery",
    "delivery.reconcile": "delivery",
    "delivery.merge": "merge_request",
    "delegation.publish_result": "delegation",
    "delegation.wake_waiters": "wait_subscription",
    "delegation.cancel": "delegation",
    "connection.validate": "connection",
    "connection.provision": "connection",
    "credential.refresh": "connection",
    "service.ensure": "service_desire",
    "service.stop": "service_desire",
    "retention.cleanup": "session",
    "outbox.deliver": "outbox",
}

TARGET_COLUMNS: dict[str, str] = {
    "turn": "turn_id",
    "execution": "execution_id",
    "lease": "lease_id",
    "project_version": "project_version_id",
    "snapshot": "snapshot_id",
    "changeset": "changeset_id",
    "worktree_operation": "worktree_operation_id",
    "delivery": "delivery_id",
    "merge_request": "merge_request_id",
    "delegation": "delegation_id",
    "wait_subscription": "wait_subscription_id",
    "connection": "connection_id",
    "service_desire": "service_desire_id",
    "session": "session_id",
    "outbox": "outbox_id",
}

# Cancellation/cleanup outranks new dispatch (RFC 04).
PRIORITY = {
    "cleanup": 90,
    "cancel": 80,
    "reconcile": 60,
    "effect": 50,
    "dispatch": 40,
    "validate": 30,
}
