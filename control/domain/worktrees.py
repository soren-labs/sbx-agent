"""Worktree, Snapshot and barriers (RFC 167 §02/§03).

One logical mutable filesystem per Session — it survives lease replacement.
Snapshots are immutable manifests: ``environment`` (shareable cache) and
``checkpoint`` (private Session recovery) have distinct reuse policies.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .errors import DomainError, InvalidTransition, TerminalViolation


class WorktreeAvailability(enum.StrEnum):
    NONE = "none"
    RESTORING = "restoring"
    LIVE = "live"
    CHECKPOINTED = "checkpointed"
    UNAVAILABLE = "unavailable"


WORKTREE_EDGES: dict[WorktreeAvailability, frozenset[WorktreeAvailability]] = {
    WorktreeAvailability.NONE: frozenset({WorktreeAvailability.RESTORING}),
    WorktreeAvailability.RESTORING: frozenset(
        {
            WorktreeAvailability.LIVE,
            WorktreeAvailability.UNAVAILABLE,
            WorktreeAvailability.NONE,
        }
    ),
    WorktreeAvailability.LIVE: frozenset(
        {WorktreeAvailability.CHECKPOINTED, WorktreeAvailability.UNAVAILABLE}
    ),
    WorktreeAvailability.CHECKPOINTED: frozenset(
        {WorktreeAvailability.RESTORING, WorktreeAvailability.UNAVAILABLE}
    ),
    WorktreeAvailability.UNAVAILABLE: frozenset({WorktreeAvailability.RESTORING}),
}


def require_worktree_transition(
    current: WorktreeAvailability, target: WorktreeAvailability
) -> None:
    if target not in WORKTREE_EDGES[current]:
        raise InvalidTransition("worktree", current.value, target.value)


class SnapshotKind(enum.StrEnum):
    ENVIRONMENT = "environment"
    CHECKPOINT = "checkpoint"


class SnapshotState(enum.StrEnum):
    PREPARING = "preparing"
    READY = "ready"
    FAILED = "failed"


SNAPSHOT_EDGES: dict[SnapshotState, frozenset[SnapshotState]] = {
    SnapshotState.PREPARING: frozenset({SnapshotState.READY, SnapshotState.FAILED}),
    SnapshotState.READY: frozenset(),
    SnapshotState.FAILED: frozenset(),
}


def require_snapshot_transition(current: SnapshotState, target: SnapshotState) -> None:
    if current in (SnapshotState.READY, SnapshotState.FAILED):
        raise TerminalViolation("snapshot", current.value)
    if target not in SNAPSHOT_EDGES[current]:
        raise InvalidTransition("snapshot", current.value, target.value)


class WorktreeOperationKind(enum.StrEnum):
    """Exclusive mediated mutation barriers."""

    ACTIVATE = "activate"
    CAPTURE = "capture"
    CHECKPOINT = "checkpoint"
    APPLY = "apply"
    RESTORE = "restore"
    TURN = "turn"


class WorktreeOperationState(enum.StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class Worktree:
    id: str
    workspace_id: str
    session_id: str
    repository: str | None
    base_sha: str | None
    generation: int
    availability: WorktreeAvailability
    last_snapshot_id: str | None = None
    recovery_point: dict = field(default_factory=dict)
    created_at: object = None
    updated_at: object = None

    def require_no_active_barrier(self, active: int) -> None:
        if active:
            raise DomainError(
                "conflict",
                "an exclusive Worktree operation barrier is active",
                retryable=True,
            )


@dataclass
class Snapshot:
    id: str
    workspace_id: str
    kind: SnapshotKind
    state: SnapshotState
    # XOR owner: exactly one of project_version_id / worktree_id
    project_version_id: str | None = None
    worktree_id: str | None = None
    worktree_generation: int | None = None
    input_digest: str | None = None
    content_digest: str | None = None
    manifest: dict = field(default_factory=dict)
    blob_refs: list[str] = field(default_factory=list)
    backend_refs: dict = field(default_factory=dict)
    compatibility: dict = field(default_factory=dict)
    event_watermark: int | None = None
    created_at: object = None
    updated_at: object = None
