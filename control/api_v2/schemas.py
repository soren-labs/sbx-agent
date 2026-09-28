"""Typed request/response models for the Session-first ``/v2`` surface.

Request bodies map onto the V1 task declaration (``prompt`` is a plain
string here; ``repository``/``execution``/``delivery`` mirror the V1
``source``/``execution``/``delivery`` groups; everything else lives under
``advanced``). Response bodies are the sanitized projection: no agent/task/
run ids, no scheduler candidates or LRU evidence, no Modal/CLI internals,
no raw artifact refs.
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

SessionStatus = Literal["queued", "running", "finished", "failed", "cancelled"]
"""Stable public status — the only values a V2 client switches on."""

SessionPhase = Literal[
    "provisioning",
    "queued",
    "running",
    "delivering",
    "finished",
    "failed",
    "cancelled",
]
"""Finer UI-oriented phase: work in flight distinguishes queueing,
provisioning, agent execution and the post-run delivery step."""


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------


class SessionRepository(BaseModel):
    """Repo + optional ref the session works in."""

    model_config = ConfigDict(extra="forbid")

    repo: str = Field(min_length=1)
    ref: str | None = None


class SessionExecution(BaseModel):
    """Execution preferences — ``auto`` resolves through the scheduler."""

    model_config = ConfigDict(extra="forbid")

    provider: ProviderId | Literal["auto"] = "auto"
    account_id: str | Literal["auto"] | None = "auto"
    model: str | Literal["auto"] | None = "auto"
    reasoning_effort: ReasoningEffort | Literal["auto"] | None = "auto"


class SessionPullRequest(BaseModel):
    """Delivery target: open a pull request when work lands."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    body: str | None = None
    draft: bool = False
    target: str | None = None


class SessionDelivery(BaseModel):
    """Where the work goes: branch name + optional PR/publish policy."""

    model_config = ConfigDict(extra="forbid")

    branch: str | None = None
    pull_request: SessionPullRequest | None = None
    auto_publish: bool = False


class SessionAdvanced(BaseModel):
    """Optional engine-level knobs — not part of the everyday flow."""

    model_config = ConfigDict(extra="forbid")

    metadata: WorkflowMetadata | None = None
    output_contract: OutputContract | None = None
    resources: SessionResources | None = None
    compute: SessionCompute | None = None
    idle_timeout_s: int | None = Field(default=None, ge=1)


class CreateSessionRequest(BaseModel):
    """``POST /v2/sessions`` body — a prompt plus optional decorations."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    title: str | None = None
    repository: SessionRepository | None = None
    execution: SessionExecution | None = None
    delivery: SessionDelivery | None = None
    advanced: SessionAdvanced | None = None


class SessionMessageRequest(BaseModel):
    """``POST /v2/sessions/{id}/messages`` body — a follow-up turn."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    on_busy: Literal["queue", "reject"] = "queue"


class SessionRetryRequest(BaseModel):
    """``POST /v2/sessions/{id}/retry`` body.

    ``mode=None`` picks by session state: a failed delivery retries the
    publish; otherwise the original prompt (or an override) re-runs.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["delivery", "run"] | None = None
    prompt: str | None = None
    on_busy: Literal["queue", "reject"] = "queue"


class SessionDeliverRequest(BaseModel):
    """``POST /v2/sessions/{id}/deliver`` body.

    ``n`` selects a revision by its sequence number (default: latest);
    ``branch``/``pull_request`` override the declared delivery policy.
    """

    model_config = ConfigDict(extra="forbid")

    n: int | None = Field(default=None, ge=1)
    branch: str | None = None
    pull_request: SessionPullRequest | None = None


# ---------------------------------------------------------------------------
# responses — sanitized projections
# ---------------------------------------------------------------------------


class RepositoryView(BaseModel):
    repo: str
    ref: str | None = None
    base_sha: str | None = None


class ExecutionView(BaseModel):
    """The effective pick — no candidate list, no LRU evidence."""

    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None


class UsageView(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    cache_write_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None


class PullRequestView(BaseModel):
    number: int | None = None
    url: str | None = None
    state: str | None = None
    head_sha: str | None = None
    head_branch: str | None = None
    base: str | None = None
    draft: bool | None = None


class MergeView(BaseModel):
    merged: bool | None = None
    merge_commit_sha: str | None = None
    head_sha: str | None = None
    merged_at: str | None = None


class DeliveryView(BaseModel):
    required: bool
    status: Literal["pending", "delivered", "failed"] | None = None
    branch: str | None = None
    pushed_head_sha: str | None = None
    pull_request: PullRequestView | None = None
    merge: MergeView | None = None
    # A string (workspace publish error) or a structured {code, message}
    # (revision delivery error) — both already sanitized at the source.
    error: Any = None


class ChangesView(BaseModel):
    """Workspace diff state — is there materialized work to deliver?"""

    status: Literal["none", "unchanged", "ready"]
    base_sha: str | None = None
    head_sha: str | None = None
    branch: str | None = None
    pull_request: PullRequestView | None = None


class RevisionView(BaseModel):
    """A durable code-change snapshot, addressed by its sequence ``n``."""

    n: int
    status: str
    repo: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    error: str | None = None
    delivery: DeliveryView | None = None


class RunErrorView(BaseModel):
    code: str | None = None
    source: str | None = None
    message: str | None = None
    retryable: bool | None = None
    retry_after: float | None = None


class RunView(BaseModel):
    """One turn of the session in Session vocabulary."""

    n: int
    status: SessionStatus
    prompt: str | None = None
    result: str | None = None
    error: RunErrorView | None = None
    usage: UsageView | None = None
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    structured_output: Any = None
    queue_position: int | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class SessionView(BaseModel):
    """The Session resource — the only object V2 clients name."""

    id: str
    title: str | None = None
    status: SessionStatus
    phase: SessionPhase
    prompt: str
    created_at: str | None = None
    updated_at: str | None = None
    repository: RepositoryView | None = None
    execution: ExecutionView | None = None
    turns: int = 0
    usage: UsageView | None = None
    cost_estimate_usd: float | None = None
    delivery: DeliveryView | None = None
    changes: ChangesView | None = None
    error: RunErrorView | None = None


class SessionResponse(BaseModel):
    session: SessionView


class SessionListResponse(BaseModel):
    sessions: list[SessionView]
    total: int
    limit: int
    offset: int


class SessionDetailResponse(BaseModel):
    """Bounded first view: session + its most recent turns in one body."""

    session: SessionView
    runs: list[RunView]
    run_count: int
    truncated: bool


class MessageAck(BaseModel):
    """Follow-up acknowledgement — the turn number it was queued as."""

    n: int
    status: SessionStatus


class SessionMessageResponse(BaseModel):
    session: SessionView
    message: MessageAck | None = None


class SessionRetryResponse(BaseModel):
    session: SessionView
    run: RunView | None = None


class SessionChangesResponse(BaseModel):
    session: SessionView
    changes: ChangesView | None = None
    revisions: list[RevisionView] = []


class SessionDeliverResponse(BaseModel):
    session: SessionView
    revision: RevisionView | None = None
