"""SOR-256: typed request/response schemas for ``/v2``.

Every model is strict (``extra="forbid"``) so the served OpenAPI carries
``additionalProperties: false`` instead of generic blobs. Response models
mirror the projection in ``control.api_v2.projection`` — Session ids are
opaque and internal Task/Agent/Run/Revision ids never appear (the per-agent
revision sequence ``n`` and per-session message ids are session-relative
coordinates, not foreign ids).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from control.api_v1.schemas import (
    OutputContract,
    ProviderId,
    ReasoningEffort,
    SessionCompute,
    SessionResources,
    WorkflowMetadata,
)

# ---------------------------------------------------------------------------
# public enums
# ---------------------------------------------------------------------------

SessionStatus = Literal["queued", "running", "finished", "failed", "cancelled"]
SESSION_STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "finished",
    "failed",
    "cancelled",
)

SessionPhase = Literal[
    "resolving",
    "queued",
    "provisioning",
    "starting_provider",
    "running",
    "publishing",
    "finished",
    "failed",
    "cancelled",
]
SESSION_PHASES: tuple[str, ...] = (
    "resolving",
    "queued",
    "provisioning",
    "starting_provider",
    "running",
    "publishing",
    "finished",
    "failed",
    "cancelled",
)

DeliveryMode = Literal["none", "branch", "pull_request", "auto"]
DELIVERY_MODES: tuple[str, ...] = ("none", "branch", "pull_request", "auto")

EVENT_TYPES: tuple[str, ...] = (
    "session.status",
    "message.created",
    "activity.started",
    "activity.updated",
    "activity.completed",
    "usage.updated",
    "changes.updated",
    "delivery.updated",
    "session.completed",
    "session.failed",
)

# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------


class SessionRepository(BaseModel):
    """Repo + optional ref; ``ref`` defaults to the repo's default branch."""

    model_config = ConfigDict(extra="forbid")

    repo: str = Field(min_length=1)
    ref: str | None = None


class SessionExecution(BaseModel):
    """Execution preferences — ``auto`` resolves through the scheduler."""

    model_config = ConfigDict(extra="forbid")

    provider: ProviderId | Literal["auto"] = "auto"
    model: str | Literal["auto"] | None = "auto"
    reasoning_effort: ReasoningEffort | Literal["auto"] | None = "auto"
    account_id: str | Literal["auto"] | None = "auto"


class SessionDelivery(BaseModel):
    """Where the work lands when it finishes (or when ``POST .../deliver`` runs).

    ``mode`` selects the policy:

    - ``none`` — no remote delivery; work stays in the sandbox revision.
    - ``branch`` — ``target`` names the remote branch a deliver pushes to.
    - ``pull_request`` — a deliver pushes the work branch (``branch`` or a
      session-scoped default) and opens/updates a PR against ``target``.
    - ``auto`` — same as ``pull_request``, but the publish fires
      automatically when the run finishes.
    """

    model_config = ConfigDict(extra="forbid")

    mode: DeliveryMode = "none"
    target: str | None = None
    draft: bool = False
    branch: str | None = None
    title: str | None = None
    body: str | None = None


class CreateSessionRequest(BaseModel):
    """Session declaration — ``prompt`` plus optional titles and preferences."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    title: str | None = None
    repository: SessionRepository | None = None
    execution: SessionExecution | None = None
    delivery: SessionDelivery | None = None
    metadata: WorkflowMetadata | None = None
    output_contract: OutputContract | None = None
    resources: SessionResources | None = None
    compute: SessionCompute | None = None
    idle_timeout_s: int | None = Field(default=None, ge=1)


class CreateMessageRequest(BaseModel):
    """A follow-up message on the session."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    metadata: WorkflowMetadata | None = None
    output_contract: OutputContract | None = None


class RetrySessionRequest(BaseModel):
    """Retry the session's failed step.

    ``mode=None`` picks by session state: a failed delivery retries the
    publish; otherwise the session's prompt (or the ``prompt`` override)
    re-runs on the same session.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["run", "delivery"] | None = None
    prompt: str | None = Field(default=None, min_length=1)


class DeliverSessionRequest(BaseModel):
    """Optional delivery overrides for a manual publish."""

    model_config = ConfigDict(extra="forbid")

    branch: str | None = None
    target: str | None = None
    draft: bool | None = None
    title: str | None = None
    body: str | None = None


# ---------------------------------------------------------------------------
# response views
# ---------------------------------------------------------------------------


class SessionErrorView(BaseModel):
    """Machine-readable session failure detail (canonical run error shape)."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    source: str | None = None
    retryable: bool = False
    retry_after: float | None = None


class SessionExecutionView(BaseModel):
    """The execution the session resolved to (``None`` while resolving)."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    account_id: str | None = None


class SessionRepositoryView(BaseModel):
    """The repository the session is bound to (resolved ``base_sha`` when known)."""

    model_config = ConfigDict(extra="forbid")

    repo: str
    ref: str | None = None
    base_sha: str | None = None


class SessionUsageView(BaseModel):
    """Cumulative token usage; ``None`` when never reported."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    cache_write_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None


class SessionMessageView(BaseModel):
    """One session message; ``id`` is session-relative (``msg-<n>``)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    role: Literal["user", "assistant"]
    text: str
    created_at: str | None = None


class SessionActivityView(BaseModel):
    """One normalized activity row; flat typed fields per kind."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    status: str
    summary: str | None = None
    command: str | None = None
    path: str | None = None
    text: str | None = None
    message: str | None = None
    exit_code: int | None = None


class SessionPullRequestView(BaseModel):
    """The delivered pull request, when one exists."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    number: int | None = None
    state: str | None = None
    draft: bool | None = None
    base: str | None = None


class SessionDeliveryView(BaseModel):
    """Session-level delivery state — never Revision/PR internals ids."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["none", "pending", "delivered", "failed"]
    mode: str | None = None
    branch: str | None = None
    pushed_head_sha: str | None = None
    pull_request: SessionPullRequestView | None = None
    error: str | None = None


class SessionRevisionView(BaseModel):
    """One materialized work product (session-relative sequence ``n``)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    n: int
    status: str
    base_sha: str | None = None
    head_sha: str | None = None
    created_at: str | None = None
    error: str | None = None


class SessionChangesView(BaseModel):
    """Session-level changes: is there work to deliver?"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["none", "unchanged", "ready", "materialization_failed"]
    repo: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    count: int = 0
    revisions: list[SessionRevisionView] = Field(default_factory=list)


class SessionSummaryView(BaseModel):
    """The bounded public session row (list + mutation responses)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str | None = None
    status: SessionStatus
    phase: SessionPhase
    execution: SessionExecutionView = Field(default_factory=SessionExecutionView)
    repository: SessionRepositoryView | None = None
    usage: SessionUsageView | None = None
    error: SessionErrorView | None = None
    created_at: str | None = None
    updated_at: str | None = None


class SessionDetailView(SessionSummaryView):
    """Bounded first-view projection — summary + recent context, no waterfall."""

    model_config = ConfigDict(extra="forbid")

    prompt: str | None = None
    messages: list[SessionMessageView] = Field(default_factory=list)
    activities: list[SessionActivityView] = Field(default_factory=list)
    changes: SessionChangesView | None = None
    delivery: SessionDeliveryView | None = None
    cost_estimate_usd: float = 0.0


# ---------------------------------------------------------------------------
# envelopes
# ---------------------------------------------------------------------------


class SessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: SessionSummaryView


class SessionDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: SessionDetailView


class SessionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sessions: list[SessionSummaryView]
    next_cursor: str | None = None


class SessionMessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: SessionSummaryView
    accepted: bool = True
    message: SessionMessageView | None = None


class SessionChangesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: SessionChangesView


class SessionDeliverResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: SessionSummaryView
    delivery: SessionDeliveryView | None = None
    accepted: bool = True


# ---------------------------------------------------------------------------
# canonical event payloads (data field of each normalized SSE frame)
# ---------------------------------------------------------------------------


class SessionStatusEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SessionStatus
    phase: SessionPhase


class MessageCreatedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: SessionMessageView


class ActivityEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activity: SessionActivityView


class UsageUpdatedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usage: SessionUsageView = Field(default_factory=SessionUsageView)
    run: int | None = None


class ChangesUpdatedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: SessionChangesView


class DeliveryUpdatedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery: SessionDeliveryView


class SessionTerminalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["finished", "failed", "cancelled"]
    error: SessionErrorView | None = None


def _model_json(model: BaseModel) -> dict[str, Any]:
    """Event payload dict — ``None`` fields omitted to keep frames small."""
    return model.model_dump(mode="json", exclude_none=True)
