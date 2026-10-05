"""Durable Jobs, claims, fences and outbox (RFC 167 §04).

All slow/recoverable effects use one Job mechanism with transactional
claims, generation fencing and dedupe. Jobs are never user work identity.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class JobKind(enum.StrEnum):
    TURN_DISPATCH = "turn.dispatch"
    EXECUTION_RECONCILE = "execution.reconcile"
    EXECUTOR_ALLOCATE = "executor.allocate"
    EXECUTOR_RECONCILE = "executor.reconcile"
    EXECUTOR_RELEASE = "executor.release"
    ENVIRONMENT_BUILD = "environment.build"
    WORKTREE_RESTORE = "worktree.restore"
    SNAPSHOT_CAPTURE = "snapshot.capture"
    CHANGESET_CAPTURE = "changeset.capture"
    CHANGESET_APPLY = "changeset.apply"
    DELIVERY_PERFORM = "delivery.perform"
    DELIVERY_RECONCILE = "delivery.reconcile"
    DELIVERY_MERGE = "delivery.merge"
    DELEGATION_PUBLISH_RESULT = "delegation.publish_result"
    DELEGATION_WAKE_WAITERS = "delegation.wake_waiters"
    DELEGATION_CANCEL = "delegation.cancel"
    CONNECTION_VALIDATE = "connection.validate"
    CONNECTION_PROVISION = "connection.provision"
    CREDENTIAL_REFRESH = "credential.refresh"
    SERVICE_ENSURE = "service.ensure"
    SERVICE_STOP = "service.stop"
    RETENTION_CLEANUP = "retention.cleanup"
    OUTBOX_DELIVER = "outbox.deliver"


class JobState(enum.StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


JOB_TERMINAL: frozenset[JobState] = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}
)

JOB_CLAIMABLE: frozenset[JobState] = frozenset({JobState.QUEUED, JobState.RETRY_WAIT})


class TargetFamily(enum.StrEnum):
    """Validated job target family — exactly one matching typed FK."""

    TURN = "turn"
    EXECUTION = "execution"
    EXECUTOR_LEASE = "executor_lease"
    SNAPSHOT = "snapshot"
    CHANGESET = "changeset"
    DELIVERY = "delivery"
    MERGE_REQUEST = "merge_request"
    DELEGATION = "delegation"
    CONNECTION = "connection"
    CREDENTIAL_VERSION = "credential_version"
    SERVICE_INSTANCE = "service_instance"
    SERVICE_DESIRE = "service_desire"
    WORKTREE = "worktree"
    SESSION = "session"
    PROJECT_VERSION = "project_version"
    ENVIRONMENT_BUILD = "environment_build"
    OUTBOX_MESSAGE = "outbox_message"
    NONE = "none"


TARGET_FK_COLUMN: dict[TargetFamily, str] = {
    TargetFamily.TURN: "turn_id",
    TargetFamily.EXECUTION: "execution_id",
    TargetFamily.EXECUTOR_LEASE: "executor_lease_id",
    TargetFamily.SNAPSHOT: "snapshot_id",
    TargetFamily.CHANGESET: "changeset_id",
    TargetFamily.DELIVERY: "delivery_id",
    TargetFamily.MERGE_REQUEST: "merge_request_id",
    TargetFamily.DELEGATION: "delegation_id",
    TargetFamily.CONNECTION: "connection_id",
    TargetFamily.CREDENTIAL_VERSION: "credential_version_id",
    TargetFamily.SERVICE_INSTANCE: "service_instance_id",
    TargetFamily.SERVICE_DESIRE: "service_desire_id",
    TargetFamily.WORKTREE: "worktree_id",
    TargetFamily.SESSION: "session_id",
    TargetFamily.PROJECT_VERSION: "project_version_id",
    TargetFamily.ENVIRONMENT_BUILD: "environment_build_id",
    TargetFamily.OUTBOX_MESSAGE: "outbox_message_id",
    TargetFamily.NONE: None,
}


@dataclass
class Job:
    id: str
    workspace_id: str
    kind: JobKind
    target_family: TargetFamily
    target_id: str | None
    effect_id: str
    dedupe_key: str
    payload: dict = field(default_factory=dict)
    priority: int = 100
    due_at: object = None
    deadline_at: object = None
    attempt_limit: int = 8
    attempts: int = 0
    state: JobState = JobState.QUEUED
    claim_generation: int = 0
    claim_holder: str | None = None
    claim_expires_at: object = None
    last_error: dict | None = None
    result: dict | None = None
    created_at: object = None
    updated_at: object = None
