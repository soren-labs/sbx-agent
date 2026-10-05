"""Committed append-only Session journal (RFC 167 §04).

One committed sequence per Session; synchronous typed projections are the
current view. The envelope vocabulary below is canonical.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

ENVELOPE_SCHEMA_VERSION = 1


class EventSource(enum.StrEnum):
    APPLICATION = "application"
    RUNTIME = "runtime"
    SYSTEM = "system"
    IMPORT = "import"


# Canonical event names → emitting owner application.
EVENT_TYPES: dict[str, str] = {
    "session.created": "sessions",
    "session.settings_changed": "sessions",
    "session.archived": "sessions",
    "session.unarchived": "sessions",
    "session.closed": "sessions",
    "message.accepted": "sessions",
    "message.routed": "sessions",
    "message.part_added": "sessions",
    "message.part_updated": "sessions",
    "message.completed": "sessions",
    "turn.queued": "sessions",
    "turn.preparing": "execution",
    "turn.started": "execution",
    "turn.cancel_requested": "sessions",
    "turn.succeeded": "execution",
    "turn.failed": "execution",
    "turn.cancelled": "execution",
    "turn.interrupted": "execution",
    "execution.preparing": "execution",
    "execution.started": "execution",
    "execution.native_bound": "execution",
    "execution.observed_terminal": "ingest",
    "execution.stopped": "execution",
    "tool.started": "ingest",
    "tool.updated": "ingest",
    "tool.completed": "ingest",
    "usage.observed": "ingest",
    "diagnostic.reported": "ingest",
    "executor.bound": "execution",
    "executor.quiescing": "execution",
    "executor.released": "execution",
    "executor.unavailable": "execution",
    "worktree.restored": "worktrees",
    "worktree.changed": "worktrees",
    "worktree.apply_requested": "worktrees",
    "worktree.applied": "worktrees",
    "snapshot.requested": "worktrees",
    "snapshot.ready": "worktrees",
    "snapshot.failed": "worktrees",
    "changeset.capture_requested": "changes",
    "changeset.ready": "changes",
    "changeset.capture_failed": "changes",
    "delivery.requested": "delivery",
    "delivery.progressed": "delivery",
    "delivery.blocked": "delivery",
    "delivery.succeeded": "delivery",
    "delivery.failed": "delivery",
    "delivery.cancelled": "delivery",
    "delivery.merge_requested": "delivery",
    "delivery.merged": "delivery",
    "delivery.merge_failed": "delivery",
    "delegation.created": "delegation",
    "delegation.waiting": "delegation",
    "delegation.result_published": "delegation",
    "delegation.failed": "delegation",
    "delegation.cancel_requested": "delegation",
    "delegation.cancelled": "delegation",
    "service.requested": "services",
    "service.ready": "services",
    "service.degraded": "services",
    "service.failed": "services",
    "service.stopped": "services",
    "history.imported": "import",
    # Later: automation.fired / webhook.dispatched / extension.observed
}

TERMINAL_TURN_EVENTS: frozenset[str] = frozenset(
    {"turn.succeeded", "turn.failed", "turn.cancelled", "turn.interrupted"}
)


@dataclass(frozen=True)
class EventActor:
    kind: str  # user|api_key|session|system|worker|runtime|import
    principal_id: str | None = None
    session_id: str | None = None
    worker_id: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class EventEnvelope:
    """One committed fact. ``seq`` is the committed per-Session order."""

    id: str
    workspace_id: str
    session_id: str
    seq: int
    type: str
    schema_version: int
    recorded_at: object
    actor: EventActor
    source: EventSource
    payload: dict = field(default_factory=dict)
    observed_at: object = None
    causation_id: str | None = None
    correlation_id: str | None = None
    turn_id: str | None = None
    execution_id: str | None = None
    executor_lease_id: str | None = None
    lease_generation: int | None = None
    delegation_id: str | None = None
    changeset_id: str | None = None
    delivery_id: str | None = None
    # Runtime-source fields
    runtime_epoch: str | None = None
    local_seq: int | None = None
    adapter_version: str | None = None
    cli_version: str | None = None

    def to_public(self) -> dict:
        d = {
            "id": self.id,
            "session_id": self.session_id,
            "seq": self.seq,
            "type": self.type,
            "schema_version": self.schema_version,
            "recorded_at": self.recorded_at,
            "actor": self.actor.to_dict(),
            "source": self.source.value,
            "payload": self.payload,
        }
        for k in (
            "observed_at",
            "causation_id",
            "correlation_id",
            "turn_id",
            "execution_id",
            "executor_lease_id",
            "lease_generation",
            "delegation_id",
            "changeset_id",
            "delivery_id",
            "runtime_epoch",
            "local_seq",
        ):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d
