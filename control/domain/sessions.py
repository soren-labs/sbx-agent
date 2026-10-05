"""Session, Message and Turn — durable work identity (RFC 167 §02).

Session is the durable product identity. It is never a machine, native thread,
PR or API-version wrapper. Activity is a query projection, not a lifecycle.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .errors import DomainError, InvalidTransition, TerminalViolation


class SessionLifecycle(enum.StrEnum):
    OPEN = "open"
    ARCHIVED = "archived"
    CLOSED = "closed"  # terminal, irreversible


class SessionRole(enum.StrEnum):
    """Roles are policy/instruction/result-contract values, all ordinary Sessions."""

    AUTHOR = "author"
    DEVELOPER = "developer"
    REVIEWER = "reviewer"
    TESTER = "tester"
    RESEARCHER = "researcher"
    SECURITY = "security"
    INTEGRATOR = "integrator"
    COORDINATOR = "coordinator"


class Activity(enum.StrEnum):
    """Projection-only signal; never a second lifecycle."""

    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    ATTENTION = "attention"


class MessageRouting(enum.StrEnum):
    NOTE = "note"
    QUEUE = "queue"
    STEER = "steer"


class TurnState(enum.StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TURN_TERMINAL: frozenset[TurnState] = frozenset(
    {TurnState.SUCCEEDED, TurnState.FAILED, TurnState.CANCELLED, TurnState.INTERRUPTED}
)
TURN_ACTIVE: frozenset[TurnState] = frozenset(
    {TurnState.PREPARING, TurnState.RUNNING, TurnState.CANCELLING}
)

# RFC 167 §02: queued→preparing/cancelled; preparing→queued/running/failed/
# cancelling; running→succeeded/failed/cancelling/interrupted;
# cancelling→cancelled/interrupted. Terminal states never change.
TURN_EDGES: dict[TurnState, frozenset[TurnState]] = {
    TurnState.QUEUED: frozenset({TurnState.PREPARING, TurnState.CANCELLED}),
    TurnState.PREPARING: frozenset(
        {TurnState.QUEUED, TurnState.RUNNING, TurnState.FAILED, TurnState.CANCELLING}
    ),
    TurnState.RUNNING: frozenset(
        {
            TurnState.SUCCEEDED,
            TurnState.FAILED,
            TurnState.CANCELLING,
            TurnState.INTERRUPTED,
        }
    ),
    TurnState.CANCELLING: frozenset({TurnState.CANCELLED, TurnState.INTERRUPTED}),
    TurnState.SUCCEEDED: frozenset(),
    TurnState.FAILED: frozenset(),
    TurnState.CANCELLED: frozenset(),
    TurnState.INTERRUPTED: frozenset(),
}

TURN_REASONS: frozenset[str] = frozenset(
    {
        "unsupported_capability",
        "credential_invalid",
        "waiting_capacity",
        "runtime_incompatible",
        "context_mismatch",
        "outcome_unknown",
        "cancel_requested",
        "deadline_exceeded",
        "harness_error",
        "provider_error",
        "contract_invalid",
        "capture_failed",
    }
)


def require_turn_transition(current: TurnState, target: TurnState) -> None:
    if current in TURN_TERMINAL:
        raise TerminalViolation("turn", current.value)
    if target not in TURN_EDGES[current]:
        raise InvalidTransition("turn", current.value, target.value)


@dataclass(frozen=True)
class HarnessBinding:
    """Pinned Harness lineage for a Session. Model/effort may vary per Turn
    inside verified capability support; the provider lineage never varies."""

    provider_id: str
    model: str | None = None
    effort: str | None = None
    adapter_version: str | None = None
    cli_version: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass(frozen=True)
class MessageAuthor:
    kind: str  # user|api_key|session|system|import
    principal_id: str | None = None
    session_id: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass(frozen=True)
class MessageContent:
    """Immutable accepted input content."""

    kind: str  # text|file_ref|attachment_ref
    text: str | None = None
    blob_id: str | None = None
    name: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass(frozen=True)
class ResultContract:
    """Versioned typed result requirement (RFC 167 §05)."""

    kind: str  # ReviewAssessment|TestResult|ResearchResult|IntegrationResult|GenericResult
    schema_version: int = 1
    schema_digest: str | None = None
    enforcement: str = "required"  # required|advisory
    required_subject_digest: str | None = None
    required_head_sha: str | None = None
    evidence_requirements: tuple[str, ...] = ()
    completion_policy: str = "succeed_or_fail"

    RESULT_KINDS: frozenset[str] = frozenset(
        {
            "ReviewAssessment",
            "TestResult",
            "ResearchResult",
            "IntegrationResult",
            "GenericResult",
        }
    )

    def __post_init__(self) -> None:
        if self.kind not in self.RESULT_KINDS:
            raise DomainError("validation_failed", f"unknown ResultContract kind {self.kind!r}")

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "schema_digest": self.schema_digest,
            "enforcement": self.enforcement,
            "required_subject_digest": self.required_subject_digest,
            "required_head_sha": self.required_head_sha,
            "evidence_requirements": list(self.evidence_requirements),
            "completion_policy": self.completion_policy,
        }


@dataclass
class Session:
    id: str
    workspace_id: str
    role: str
    title: str | None
    labels: list[str]
    created_by: str
    lifecycle: SessionLifecycle
    project_version_id: str | None
    harness: HarnessBinding
    effective_input: dict
    effective_input_digest: str
    linked_from_session_id: str | None
    version: int
    next_event_seq: int
    next_message_ordinal: int
    next_turn_ordinal: int
    active_turn_id: str | None
    active_lease_id: str | None
    spec: dict = field(default_factory=dict)  # projectless spec when no pver
    created_at: object = None
    updated_at: object = None
    closed_at: object = None

    def require_writable(self) -> None:
        if self.lifecycle is SessionLifecycle.CLOSED:
            raise TerminalViolation("session", self.lifecycle.value)

    def require_accepting_work(self) -> None:
        if self.lifecycle is SessionLifecycle.CLOSED:
            raise TerminalViolation("session", "closed")
        if self.lifecycle is SessionLifecycle.ARCHIVED:
            raise DomainError(
                "invalid_state",
                "session is archived; unarchive before queueing work",
                action="unarchive",
            )
