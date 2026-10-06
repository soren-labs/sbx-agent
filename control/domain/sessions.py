"""Session, Message and Turn rules (RFC 02 lifecycle and continuation rules)."""

from __future__ import annotations

from control.domain.errors import DomainError
from control.domain.lifecycle import Lifecycle, table

SESSION = Lifecycle(
    "session",
    table({"open": ("archived", "closed"), "archived": ("open", "closed")}),
    frozenset({"closed"}),
)

TURN = Lifecycle(
    "turn",
    table(
        {
            "queued": ("preparing", "cancelled"),
            "preparing": ("queued", "running", "failed", "cancelling"),
            "running": ("succeeded", "failed", "cancelling", "interrupted"),
            "cancelling": ("cancelled", "interrupted"),
        }
    ),
    frozenset({"succeeded", "failed", "cancelled", "interrupted"}),
)

ACTIVE_TURN_STATES = frozenset({"preparing", "running", "cancelling"})
ROLES = frozenset(
    {"developer", "review", "test", "research", "security", "integration", "coordinator"}
)
ROUTINGS = frozenset({"note", "queue", "steer"})
ACTIVITY = ("idle", "queued", "running", "awaiting_input", "attention")

# Distinct reasons on queued/preparing/failed Turns (RFC 02).
TURN_REASONS = frozenset(
    {
        "waiting_capacity",
        "waiting_executor",
        "unsupported_capability",
        "credential_invalid",
        "connection_revoked",
        "connection_required",
        "runtime_incompatible",
        "executor_unavailable",
        "context_mismatch",
        "context_unavailable",
        "outcome_unknown",
        "output_contract_invalid",
        "provider_failed",
        "deadline_exceeded",
        "cancelled_by_user",
        "session_closed",
    }
)


def check_accepts_work(lifecycle: str) -> None:
    if lifecycle != "open":
        raise DomainError(
            "invalid_transition",
            f"session is {lifecycle}; new execution requests are rejected",
            details={"lifecycle": lifecycle},
        )


def check_role(role: str) -> None:
    if role not in ROLES:
        raise DomainError("validation_failed", f"unknown role {role}", details={"field": "role"})


def check_routing(routing: str) -> None:
    if routing not in ROUTINGS:
        raise DomainError("validation_failed", "routing must be note, queue or steer")


def activity_of(
    lifecycle: str, active_turn_state: str | None, queued: int, last_terminal: str | None
) -> str:
    """Query projection only; never a second lifecycle."""
    if active_turn_state in ACTIVE_TURN_STATES:
        return "running"
    if queued:
        return "queued"
    if last_terminal in ("failed", "interrupted"):
        return "attention"
    if last_terminal == "succeeded" and lifecycle == "open":
        return "awaiting_input"
    return "idle"
