"""Delivery intent, steps, target claims and merge gates (RFC 167 §05).

Delivery owns authorized external effects for one exact ChangeSet subject.
Push/merge ambiguity stays unresolved — never speculative repeats.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .errors import InvalidTransition, TerminalViolation


class DeliveryState(enum.StrEnum):
    PENDING = "pending"
    EXECUTING = "executing"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


DELIVERY_TERMINAL: frozenset[DeliveryState] = frozenset(
    {DeliveryState.SUCCEEDED, DeliveryState.CANCELLED}
)

DELIVERY_EDGES: dict[DeliveryState, frozenset[DeliveryState]] = {
    DeliveryState.PENDING: frozenset(
        {
            DeliveryState.EXECUTING,
            DeliveryState.BLOCKED,
            DeliveryState.CANCELLED,
        }
    ),
    DeliveryState.EXECUTING: frozenset(
        {DeliveryState.BLOCKED, DeliveryState.SUCCEEDED, DeliveryState.FAILED}
    ),
    DeliveryState.BLOCKED: frozenset({DeliveryState.EXECUTING, DeliveryState.CANCELLED}),
    DeliveryState.FAILED: frozenset({DeliveryState.PENDING}),  # authorized retry of same intent
    DeliveryState.SUCCEEDED: frozenset(),
    DeliveryState.CANCELLED: frozenset(),
}


def require_delivery_transition(current: DeliveryState, target: DeliveryState) -> None:
    if current in DELIVERY_TERMINAL:
        raise TerminalViolation("delivery", current.value)
    if target not in DELIVERY_EDGES[current]:
        raise InvalidTransition("delivery", current.value, target.value)


class DeliveryTransport(enum.StrEnum):
    EXPORT = "export"
    GIT_BRANCH = "git_branch"
    PULL_REQUEST = "pull_request"
    DIRECT_BASE = "direct_base"


class MergeRequestState(enum.StrEnum):
    PENDING = "pending"
    EXECUTING = "executing"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


MERGE_TERMINAL: frozenset[MergeRequestState] = frozenset(
    {MergeRequestState.SUCCEEDED, MergeRequestState.CANCELLED}
)

MERGE_EDGES: dict[MergeRequestState, frozenset[MergeRequestState]] = {
    MergeRequestState.PENDING: frozenset(
        {MergeRequestState.EXECUTING, MergeRequestState.BLOCKED, MergeRequestState.CANCELLED}
    ),
    MergeRequestState.EXECUTING: frozenset(
        {MergeRequestState.BLOCKED, MergeRequestState.SUCCEEDED, MergeRequestState.FAILED}
    ),
    MergeRequestState.BLOCKED: frozenset(
        {MergeRequestState.EXECUTING, MergeRequestState.CANCELLED}
    ),
    MergeRequestState.FAILED: frozenset({MergeRequestState.PENDING}),
    MergeRequestState.SUCCEEDED: frozenset(),
    MergeRequestState.CANCELLED: frozenset(),
}


def require_merge_transition(current: MergeRequestState, target: MergeRequestState) -> None:
    if current in MERGE_TERMINAL:
        raise TerminalViolation("merge_request", current.value)
    if target not in MERGE_EDGES[current]:
        raise InvalidTransition("merge_request", current.value, target.value)


@dataclass(frozen=True)
class ShipPolicy:
    """Pinned shipping policy (RFC 167 §05). Defaults: no automatic merge,
    branch/draft PR, one independent approving ReviewAssessment, declared
    required checks, base stability unpinned."""

    transport: str = "pull_request"  # export|git_branch|pull_request|direct_base
    default_target: str | None = None
    base_branch: str = "main"
    draft: bool = True
    automatic_delivery: bool = False
    required_result_roles: tuple[str, ...] = ("reviewer",)
    required_result_count: int = 1
    require_independent: bool = True
    required_check_names: tuple[str, ...] = ()
    accepted_merge_methods: tuple[str, ...] = ("merge", "squash", "rebase")
    require_base_unchanged: bool = False

    def to_dict(self) -> dict:
        return {
            "transport": self.transport,
            "default_target": self.default_target,
            "base_branch": self.base_branch,
            "draft": self.draft,
            "automatic_delivery": self.automatic_delivery,
            "required_result_roles": list(self.required_result_roles),
            "required_result_count": self.required_result_count,
            "require_independent": self.require_independent,
            "required_check_names": list(self.required_check_names),
            "accepted_merge_methods": list(self.accepted_merge_methods),
            "require_base_unchanged": self.require_base_unchanged,
        }

    @staticmethod
    def from_dict(d: dict | None) -> ShipPolicy:
        if not d:
            return ShipPolicy()
        d = dict(d)
        for k in ("required_result_roles", "required_check_names", "accepted_merge_methods"):
            if k in d and isinstance(d[k], list):
                d[k] = tuple(d[k])
        return ShipPolicy(**{k: v for k, v in d.items() if k in ShipPolicy.__dataclass_fields__})


def stable_branch_ref(session_id: str, changeset_id: str) -> str:
    """Deterministic remote ref for a subject."""
    return f"sbx/{session_id}/{changeset_id}"


@dataclass
class Delivery:
    id: str
    workspace_id: str
    session_id: str
    changeset_id: str
    subject_digest: str
    target: dict  # repository, ref, optional existing PR
    transport: DeliveryTransport
    ship_policy: dict
    authorizing_principal: str
    connection_id: str | None
    expected_remote: dict = field(default_factory=dict)  # head/base preconditions
    state: DeliveryState = DeliveryState.PENDING
    version: int = 1
    effect_evidence: dict = field(default_factory=dict)
    created_at: object = None
    updated_at: object = None
