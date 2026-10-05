"""Execution attempts and ExecutorLeases (RFC 167 §02/§03).

An Execution is an attempt to perform one Turn — operational evidence, not
another work unit. An ExecutorLease is a replaceable location; at most one is
active per Session. Backend handles are opaque metadata.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .errors import InvalidTransition, TerminalViolation


class ExecutionState(enum.StrEnum):
    PREPARING = "preparing"
    STARTED = "started"
    STOP_REQUESTED = "stop_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"  # terminal only after exhausted recovery


EXECUTION_TERMINAL: frozenset[ExecutionState] = frozenset(
    {
        ExecutionState.SUCCEEDED,
        ExecutionState.FAILED,
        ExecutionState.CANCELLED,
        ExecutionState.UNKNOWN,
    }
)

EXECUTION_EDGES: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.PREPARING: frozenset(
        {
            ExecutionState.STARTED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
            ExecutionState.UNKNOWN,
        }
    ),
    ExecutionState.STARTED: frozenset(
        {
            ExecutionState.STOP_REQUESTED,
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.UNKNOWN,
        }
    ),
    ExecutionState.STOP_REQUESTED: frozenset({ExecutionState.CANCELLED, ExecutionState.UNKNOWN}),
    ExecutionState.SUCCEEDED: frozenset(),
    ExecutionState.FAILED: frozenset(),
    ExecutionState.CANCELLED: frozenset(),
    ExecutionState.UNKNOWN: frozenset(),
}


def require_execution_transition(current: ExecutionState, target: ExecutionState) -> None:
    if current in EXECUTION_TERMINAL:
        raise TerminalViolation("execution", current.value)
    if target not in EXECUTION_EDGES[current]:
        raise InvalidTransition("execution", current.value, target.value)


class LeaseState(enum.StrEnum):
    ALLOCATING = "allocating"
    READY = "ready"
    QUIESCING = "quiescing"
    RELEASED = "released"
    LOST = "lost"


LEASE_TERMINAL: frozenset[LeaseState] = frozenset({LeaseState.RELEASED, LeaseState.LOST})

LEASE_EDGES: dict[LeaseState, frozenset[LeaseState]] = {
    LeaseState.ALLOCATING: frozenset({LeaseState.READY, LeaseState.QUIESCING, LeaseState.LOST}),
    LeaseState.READY: frozenset({LeaseState.QUIESCING, LeaseState.LOST}),
    LeaseState.QUIESCING: frozenset({LeaseState.READY, LeaseState.RELEASED, LeaseState.LOST}),
    LeaseState.RELEASED: frozenset(),
    LeaseState.LOST: frozenset(),
}


def require_lease_transition(current: LeaseState, target: LeaseState) -> None:
    if current in LEASE_TERMINAL:
        raise TerminalViolation("executor_lease", current.value)
    if target not in LEASE_EDGES[current]:
        raise InvalidTransition("executor_lease", current.value, target.value)


class ExecutorBackendKind(enum.StrEnum):
    MODAL = "modal"
    LOCAL = "local"


@dataclass(frozen=True)
class NativeContextBinding:
    """Verified native CLI context for resume."""

    provider_id: str
    native_id: str
    lineage_id: str
    cli_version: str | None = None
    adapter_version: str | None = None
    state_manifest_digest: str | None = None
    account_affinity: str | None = None
    checkpoint_ref: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Execution:
    id: str
    workspace_id: str
    session_id: str
    turn_id: str
    attempt_ordinal: int
    operation_id: str
    state: ExecutionState
    executor_lease_id: str | None = None
    lease_generation: int | None = None
    runtime_epoch: str | None = None
    credential_version_id: str | None = None
    native_binding_id: str | None = None
    cli_version: str | None = None
    adapter_version: str | None = None
    image_ref: str | None = None
    runtime_version: str | None = None
    final_watermark: int | None = None
    outcome_evidence: dict = field(default_factory=dict)
    reason: str | None = None
    created_at: object = None
    updated_at: object = None


@dataclass
class ExecutorLease:
    id: str
    workspace_id: str
    session_id: str
    backend: str
    generation: int
    state: LeaseState
    handle: dict = field(default_factory=dict)  # opaque backend metadata
    expires_at: object = None
    image_fingerprint: str | None = None
    protocol_fingerprint: str | None = None
    allocation_operation_id: str | None = None
    observed_at: object = None
    created_at: object = None
    updated_at: object = None
