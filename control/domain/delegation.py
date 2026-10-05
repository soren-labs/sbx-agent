"""Generic Delegation and validated results (RFC 167 §05).

Review/test/research/security/integration are ordinary child Sessions created
through Delegation. A DelegationResult is a validated immutable result, not an
execution subsystem.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .errors import InvalidTransition, TerminalViolation


class DelegationState(enum.StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    WAITING_RESULT = "waiting_result"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


DELEGATION_TERMINAL: frozenset[DelegationState] = frozenset(
    {DelegationState.SUCCEEDED, DelegationState.FAILED, DelegationState.CANCELLED}
)

DELEGATION_EDGES: dict[DelegationState, frozenset[DelegationState]] = {
    DelegationState.PENDING: frozenset(
        {DelegationState.ACTIVE, DelegationState.FAILED, DelegationState.CANCELLED}
    ),
    DelegationState.ACTIVE: frozenset(
        {
            DelegationState.WAITING_RESULT,
            DelegationState.SUCCEEDED,
            DelegationState.FAILED,
            DelegationState.CANCELLED,
        }
    ),
    DelegationState.WAITING_RESULT: frozenset(
        {
            DelegationState.SUCCEEDED,
            DelegationState.FAILED,
            DelegationState.CANCELLED,
        }
    ),
    DelegationState.SUCCEEDED: frozenset(),
    DelegationState.FAILED: frozenset(),
    DelegationState.CANCELLED: frozenset(),
}


def require_delegation_transition(current: DelegationState, target: DelegationState) -> None:
    if current in DELEGATION_TERMINAL:
        raise TerminalViolation("delegation", current.value)
    if target not in DELEGATION_EDGES[current]:
        raise InvalidTransition("delegation", current.value, target.value)


class WaitState(enum.StrEnum):
    PENDING = "pending"
    SATISFIED = "satisfied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


VERDICTS: frozenset[str] = frozenset({"approve", "request_changes", "comment"})


@dataclass(frozen=True)
class ReviewAssessment:
    """Validated typed review result. Subject pins are correctness-critical."""

    subject_digest: str
    verdict: str  # approve|request_changes|comment
    head_sha: str | None = None
    child_session_id: str | None = None
    completing_turn_id: str | None = None
    findings: tuple[dict, ...] = ()
    checks: tuple[dict, ...] = ()
    validation: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(f"invalid verdict {self.verdict!r}")


@dataclass
class Delegation:
    id: str
    workspace_id: str
    parent_session_id: str
    child_session_id: str
    role: str
    state: DelegationState
    result_contract: dict
    input_refs: dict = field(default_factory=dict)
    input_digest: str | None = None
    budget: dict = field(default_factory=dict)
    grant_snapshot: dict = field(default_factory=dict)
    version: int = 1
    created_at: object = None
    updated_at: object = None


@dataclass
class WaitSubscription:
    id: str
    workspace_id: str
    delegation_id: str
    subscriber_session_id: str
    predicate: dict
    state: WaitState
    deadline_at: object = None
    satisfied_by_result_id: str | None = None
    result_version: int | None = None
    created_at: object = None
