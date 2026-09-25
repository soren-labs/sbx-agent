"""SOR-225: durable Revision / Review / Delivery routes on ``/v1``.

The revision surface is task-scoped (``/v1/tasks/{id}/...``) with an
agent-scoped read for the raw agent API. ``revision`` refs accept
``"latest"``, a ``rev-…`` id, or the per-agent sequence ``n``. Delivery and
merge operate on the durable revision, so they work after the author
sandbox is gone; review is a durable resource pinned to the exact
``reviewed_head_sha``.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field

from control.api_v1 import router
from control.api_v1 import routes as _routes
from control.api_v1.deps import (
    agents_key,
    get_plane,
    get_revisions,
    get_task_store,
)
from control.api_v1.errors import V1ApiError, not_found
from control.ports import ApiKey
from control.revisions import Revision, RevisionError, RevisionService
from control.tasks import TaskStore

# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------


class RevisionPullRequest(BaseModel):
    """Deliver overrides for the PR leg (same shape as task delivery)."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    body: str | None = None
    draft: bool = False
    target: str | None = None


class DeliverRequest(BaseModel):
    """Deliver a revision: optional revision ref + delivery overrides."""

    model_config = ConfigDict(extra="forbid")

    revision: str | None = None  # "latest" | rev-… | n
    branch: str | None = None
    pull_request: RevisionPullRequest | None = None


class ReviewerRef(BaseModel):
    """Reviewer identity — the durable record of who reviewed."""

    model_config = ConfigDict(extra="forbid")

    identity: str | None = None
    agent_id: str | None = None
    run_id: str | None = None


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: str | None = None
    path: str | None = None
    line: int | None = None
    message: str = Field(min_length=1)
    code: str | None = None


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str | None = None
    verdict: Literal["approve", "request_changes", "comment"]
    findings: list[Finding] | None = None
    reviewer: ReviewerRef | None = None
    comment: str | None = None  # post a machine-readable comment on the PR


class MergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str | None = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _rev_error(exc: RevisionError) -> V1ApiError:
    return V1ApiError(exc.status_code, exc.code, exc.message)


def _require_task(task_id: str, key: ApiKey, task_store: TaskStore) -> Any:
    record = task_store.get(task_id)
    if record is None or record.owner != key.id:
        raise not_found("task not found")
    return record


def _task_agent_id(record: Any) -> str:
    if not record.agent_id:
        raise V1ApiError(409, "revision_not_found", "task has no agent yet — no revisions exist")
    return record.agent_id


def _resolve(revisions: RevisionService, agent_id: str, ref: str | None) -> Revision:
    try:
        return revisions.resolve(agent_id, ref)
    except RevisionError as exc:
        raise _rev_error(exc) from exc


def _deliver_overrides(body: DeliverRequest) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if body.branch:
        overrides["branch"] = body.branch
    if body.pull_request is not None:
        overrides["pull_request"] = body.pull_request.model_dump(exclude_none=True)
    return overrides


def _live_handle(plane: Any, agent_id: str) -> Any | None:
    rec = plane.get(agent_id)
    return rec.handle() if rec is not None else None


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@router.get("/agents/{agent_id}/revisions")
def list_agent_revisions(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    revisions: RevisionService = Depends(get_revisions),
) -> dict[str, Any]:
    """Durable revisions produced by this agent's code-changing runs."""
    _routes._require_agent(plane, agent_id)
    rows = revisions.list(agent_id)
    return {"revisions": [r.public() for r in rows]}


@router.get("/tasks/{task_id}/revisions")
def list_task_revisions(
    task_id: str,
    key: ApiKey = Depends(agents_key),
    task_store: TaskStore = Depends(get_task_store),
    revisions: RevisionService = Depends(get_revisions),
) -> dict[str, Any]:
    """Revisions produced by the task's agent (latest is last)."""
    record = _require_task(task_id, key, task_store)
    agent_id = _task_agent_id(record)
    rows = revisions.list(agent_id)
    return {"revisions": [r.public() for r in rows]}


@router.get("/tasks/{task_id}/revisions/{ref}")
def get_task_revision(
    task_id: str,
    ref: str,
    key: ApiKey = Depends(agents_key),
    task_store: TaskStore = Depends(get_task_store),
    revisions: RevisionService = Depends(get_revisions),
) -> dict[str, Any]:
    record = _require_task(task_id, key, task_store)
    revision = _resolve(revisions, _task_agent_id(record), ref)
    return {"revision": revision.public()}


@router.post("/tasks/{task_id}/deliver")
def deliver_task(
    task_id: str,
    body: DeliverRequest | None = None,
    key: ApiKey = Depends(agents_key),
    task_store: TaskStore = Depends(get_task_store),
    revisions: RevisionService = Depends(get_revisions),
) -> dict[str, Any]:
    """Deliver a revision: push its durable payload and open/update the PR.

    Operates entirely on the revision — artifact payload + recorded
    base/head — so it works after the author sandbox is gone. A failure is
    persisted as first-class ``delivery.status="failed"`` on the revision
    and returned as an explicit API error.
    """
    record = _require_task(task_id, key, task_store)
    agent_id = _task_agent_id(record)
    revision = _resolve(revisions, agent_id, body.revision if body else None)
    try:
        revision = revisions.deliver(revision, overrides=_deliver_overrides(body) if body else None)
    except RevisionError as exc:
        raise _rev_error(exc) from exc
    return {"revision": revision.public()}


@router.get("/tasks/{task_id}/reviews")
def list_task_reviews(
    task_id: str,
    revision: str | None = None,
    key: ApiKey = Depends(agents_key),
    task_store: TaskStore = Depends(get_task_store),
    revisions: RevisionService = Depends(get_revisions),
) -> dict[str, Any]:
    """Durable reviews on the task's revisions (``?revision=`` narrows)."""
    record = _require_task(task_id, key, task_store)
    agent_id = _task_agent_id(record)
    revision_id = None
    if revision:
        revision_id = _resolve(revisions, agent_id, revision).revision_id
    rows = revisions.reviews(agent_id=agent_id, revision_id=revision_id)
    return {"reviews": [r.public() for r in rows]}


@router.post("/tasks/{task_id}/reviews", status_code=201)
def create_task_review(
    task_id: str,
    body: ReviewRequest,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    task_store: TaskStore = Depends(get_task_store),
    revisions: RevisionService = Depends(get_revisions),
) -> dict[str, Any]:
    """Record a durable review on a revision.

    ``reviewer.identity`` defaults to the calling API key; ``agent_id`` /
    ``run_id`` pin a reviewing agent/run when one exists. Independence is
    computed — a reviewer equal to the revision's own agent or run can
    never satisfy the merge gate. ``comment`` additionally posts a
    machine-readable comment on the delivered pull request.
    """
    record = _require_task(task_id, key, task_store)
    agent_id = _task_agent_id(record)
    revision = _resolve(revisions, agent_id, body.revision)
    reviewer = body.reviewer or ReviewerRef()
    reviewer_agent_id = reviewer.agent_id
    if reviewer_agent_id is not None:
        _routes._require_agent(plane, reviewer_agent_id)
    identity = reviewer.identity or (
        f"agent:{reviewer_agent_id}" if reviewer_agent_id else f"key:{key.id}"
    )
    try:
        review = revisions.add_review(
            revision,
            reviewer_identity=identity,
            reviewer_agent_id=reviewer_agent_id,
            reviewer_run_id=reviewer.run_id,
            verdict=body.verdict,
            findings=[f.model_dump(exclude_none=True) for f in (body.findings or [])],
        )
        if body.comment:
            url = revisions.post_review_comment(
                revision, body.comment, handle=_live_handle(plane, revision.agent_id)
            )
            if url:
                review.comment_url = url
    except RevisionError as exc:
        raise _rev_error(exc) from exc
    return {"review": review.public()}


@router.post("/tasks/{task_id}/merge")
def merge_task(
    task_id: str,
    body: MergeRequest | None = None,
    key: ApiKey = Depends(agents_key),
    task_store: TaskStore = Depends(get_task_store),
    revisions: RevisionService = Depends(get_revisions),
) -> dict[str, Any]:
    """Merge the revision's delivered pull request — review-gated.

    Requires a non-stale ``approve`` review that is independent (never the
    revision's own agent/run) and pinned to the exact revision head; the
    remote PR ref must still resolve to the delivered head — any drift is
    ``head_sha_mismatch`` and needs a fresh review.
    """
    record = _require_task(task_id, key, task_store)
    revision = _resolve(revisions, _task_agent_id(record), body.revision if body else None)
    try:
        revision = revisions.merge(revision)
    except RevisionError as exc:
        raise _rev_error(exc) from exc
    return {"revision": revision.public()}
