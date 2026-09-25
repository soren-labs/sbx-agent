"""SOR-222/223: public Task API routes on ``/v1``.

``Task`` is the caller-facing resource: callers declare
``prompt``/``source``/``execution``/``delivery`` — never ``base_sha``,
``account_id`` pins or git booleans — and the control plane resolves the
declaration into the existing agent machinery, so the created task runs
through the exact same ``/v1/agents`` execution path.

- ``POST /v1/tasks/preflight`` — advisory resolution only: canonicalizes
  the repo, resolves the default ref + exact base sha, runs the GitHub
  authorization/permission preflight, and evaluates every account through
  the provider/runtime/auth/health/capacity/capability filter. Nothing is
  reserved; the answer is evidence.
- ``POST /v1/tasks`` — revalidates the declaration and reserves
  authoritatively (``Scheduler.acquire`` inside the shared create path);
  the task record persists ``request`` verbatim next to ``resolved``.
- ``GET /v1/tasks`` / ``GET /v1/tasks/{taskId}`` — list/detail; ``status``
  is derived from the linked run so it stays truthful post-restart.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import Depends, Header
from pydantic import BaseModel, ConfigDict, Field

from control import tasks as taskmod
from control.api_v1 import router
from control.api_v1 import routes as _routes
from control.api_v1.deps import (
    agents_key,
    get_capabilities,
    get_plane,
    get_registry,
    get_repo_resolver,
    get_resources,
    get_run_reporter,
    get_run_states,
    get_scheduler,
    get_task_store,
    get_v1_state,
    get_workflow_service,
)
from control.api_v1.errors import V1ApiError, not_found
from control.api_v1.lifecycle import RunStateStore, request_fingerprint
from control.api_v1.schemas import (
    AgentSpec,
    CreateAgentRequest,
    OutputContract,
    Prompt,
    ProviderId,
    ReasoningEffort,
    SessionCompute,
    SessionResources,
    WorkflowMetadata,
)
from control.api_v1.state import V1State
from control.api_v1.workflows import WorkflowService
from control.ports import AccountRegistry, ApiKey, Scheduler
from control.tasks import TaskRecord, TaskRefusal, TaskStore

# Scheduling refusals worth a failover retry against the next eligible
# candidate — anything else (validation, capability) is final.
_RETRYABLE_SCHEDULE_CODES = frozenset(
    {"account_busy", "account_unavailable", "provider_exhausted", "concurrency_limit"}
)


# ---------------------------------------------------------------------------
# request models — prompt / source / execution / delivery
# ---------------------------------------------------------------------------


class TaskSource(BaseModel):
    """Repo + optional ref. ``ref`` defaults to the repo's default branch
    (``auto``/``HEAD``); a 40-hex sha pins an exact commit."""

    model_config = ConfigDict(extra="forbid")

    repo: str = Field(min_length=1)
    ref: str | None = None


class TaskExecution(BaseModel):
    """``auto`` everywhere resolves through the capability filter."""

    model_config = ConfigDict(extra="forbid")

    provider: ProviderId | Literal["auto"] = "auto"
    account_id: str | Literal["auto"] | None = "auto"
    model: str | Literal["auto"] | None = "auto"
    reasoning_effort: ReasoningEffort | Literal["auto"] | None = "auto"


class TaskPullRequest(BaseModel):
    """Delivery target: open a pull request when work lands."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    body: str | None = None
    draft: bool = False
    target: str | None = None  # defaults to the resolved base ref


class TaskDelivery(BaseModel):
    """Where the work goes: branch name + optional PR/publish policy."""

    model_config = ConfigDict(extra="forbid")

    branch: str | None = None
    pull_request: TaskPullRequest | None = None
    auto_publish: bool = False


class CreateTaskRequest(BaseModel):
    """Task declaration — no ``base_sha``/git booleans/agent knobs."""

    model_config = ConfigDict(extra="forbid")

    prompt: Prompt
    name: str | None = None
    source: TaskSource | None = None
    execution: TaskExecution | None = None
    delivery: TaskDelivery | None = None
    metadata: WorkflowMetadata | None = None
    output_contract: OutputContract | None = None
    resources: SessionResources | None = None
    compute: SessionCompute | None = None
    idle_timeout_s: int | None = Field(default=None, ge=1)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _spec(body: CreateTaskRequest) -> dict[str, Any]:
    """The plain-dict spec the resolver consumes (verbatim request echo)."""
    return body.model_dump(exclude_none=True)


def _refusal(exc: TaskRefusal) -> V1ApiError:
    return V1ApiError(exc.status_code, exc.code, exc.message, retry_after=exc.retry_after)


def _resolve(
    body: CreateTaskRequest,
    *,
    registry: AccountRegistry,
    scheduler: Scheduler,
    capabilities: Any,
    resolver: Any,
) -> taskmod.TaskResolution:
    """Run the shared resolution; refusals map to the canonical error body."""
    try:
        return taskmod.resolve_task(
            _spec(body),
            registry=registry,
            scheduler=scheduler,
            capabilities=capabilities,
            resolver=resolver,
        )
    except TaskRefusal as exc:
        raise _refusal(exc) from exc


def _candidate_spec(candidate: taskmod.AccountCandidate) -> AgentSpec:
    """The per-candidate ``AgentSpec`` — its own resolved model/effort."""
    return AgentSpec(
        provider=candidate.provider,  # type: ignore[arg-type]
        account_id=candidate.account_id,
        model=candidate.model,
        reasoning_effort=candidate.effort,  # type: ignore[arg-type]
    )


def _resolved_for(
    resolution: taskmod.TaskResolution, candidate: taskmod.AccountCandidate
) -> dict[str, Any]:
    """Durable resolved evidence for the candidate that actually ran.

    The recorded pick is attempted first, but a retryable scheduling
    refusal can fail over to a later candidate — the persisted ``resolved``
    must then describe the real account/model/effort, not the advisory one.
    """
    resolved = resolution.resolved_payload()
    if candidate is resolution.execution.pick:
        return resolved
    exe = resolved["execution"]
    exe["provider"] = candidate.provider
    exe["account_id"] = candidate.account_id
    exe["model"] = candidate.model
    exe["reasoning_effort"] = candidate.effort
    evidence = exe["evidence"]
    evidence["provider"]["resolved"] = candidate.provider
    evidence["account_id"]["resolved"] = candidate.account_id
    evidence["model"]["resolved"] = candidate.model
    evidence["model"]["source"] = candidate.model_source
    evidence["reasoning_effort"]["resolved"] = candidate.effort
    evidence["reasoning_effort"]["source"] = candidate.effort_source
    return resolved


def _agent_request(
    body: CreateTaskRequest,
    agent: AgentSpec,
) -> CreateAgentRequest:
    """Project the task body onto the agent-create request shape.

    Only the task surface maps — ``workspace``/``git`` arrive resolved and
    are passed through separately, never re-read from the caller.
    """
    return CreateAgentRequest(
        prompt=body.prompt,
        agent=agent,
        name=body.name,
        idle_timeout_s=body.idle_timeout_s,
        metadata=body.metadata,
        output_contract=body.output_contract,
        resources=body.resources,
        compute=body.compute,
    )


def _task_status(record: TaskRecord, run_states: RunStateStore, plane: Any) -> str:
    """Live task status, derived from the linked agent's run-1.

    A persisted task outlives the control plane process, so status is
    always read through the durable run seam rather than stored.
    """
    if record.agent_id is None:
        return record.status
    rec = plane.get(record.agent_id)
    if rec is not None and rec.status == "running":
        reconcile = getattr(plane, "reconcile_turn", None)
        if callable(reconcile):
            reconcile(record.agent_id)
            rec = plane.get(record.agent_id) or rec
    state = run_states.get(record.agent_id, 1)
    status = state.status if state is not None else None
    mapping = {
        "CREATING": "queued",
        "RUNNING": "running",
        "FINISHED": "finished",
        "ERROR": "error",
        "CANCELLED": "cancelled",
        "EXPIRED": "expired",
    }
    if status in mapping:
        return mapping[status]
    if rec is not None and rec.status in ("closed", "timed_out", "lost"):
        return {"closed": "cancelled", "timed_out": "expired", "lost": "error"}[rec.status]
    return record.status if record.status != "queued" or rec is None else "queued"


def _task_public(record: TaskRecord, run_states: RunStateStore, plane: Any) -> dict[str, Any]:
    out = record.public()
    out["status"] = _task_status(record, run_states, plane)
    out["prompt"] = (record.request or {}).get("prompt")
    return out


def _task_detail(
    record: TaskRecord,
    *,
    plane: Any,
    v1: V1State,
    run_states: RunStateStore,
    workflows: WorkflowService,
    scheduler: Any,
    reporter: Any,
) -> dict[str, Any]:
    """Task record + live agent/run projections (same shapes as /v1/agents)."""
    task = _task_public(record, run_states, plane)
    out: dict[str, Any] = {"task": task}
    if record.agent_id is None:
        out["agent"] = None
        out["run"] = None
        return out
    rec = plane.get(record.agent_id)
    if rec is None:
        out["agent"] = None
        out["run"] = None
        return out
    pub = plane.public(rec)
    meta = _routes._meta_for(v1, rec)
    out["agent"] = _routes._agent_payload(plane, v1, workflows, rec)
    out["run"] = _routes._run_public(
        plane,
        pub,
        rec,
        1,
        v1.cancelled(rec.id),
        meta,
        run_states,
        scheduler=scheduler,
        reporter=reporter,
    )
    return out


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@router.post("/tasks/preflight")
def task_preflight(
    body: CreateTaskRequest,
    key: ApiKey = Depends(agents_key),
    registry: AccountRegistry = Depends(get_registry),
    scheduler: Scheduler = Depends(get_scheduler),
    capabilities: Any = Depends(get_capabilities),
    resolver: Any = Depends(get_repo_resolver),
) -> dict[str, Any]:
    """Advisory resolution — no lease, no record, no side effects.

    Always 200 on a well-formed declaration: hard blockers surface as
    ``fail`` checks with the refusal's canonical code in ``error``.
    """
    try:
        resolution = taskmod.resolve_task(
            _spec(body),
            registry=registry,
            scheduler=scheduler,
            capabilities=capabilities,
            resolver=resolver,
        )
    except TaskRefusal as exc:
        out = {
            "ok": False,
            "checks": [c.public() for c in exc.checks],
            "warnings": [],
            "resolved": None,
            "error": {"code": exc.code, "message": exc.message},
        }
        if exc.candidates:
            out["resolved"] = {"execution": {"candidates": [c.public() for c in exc.candidates]}}
        return out
    return resolution.public(ok=True)


@router.post("/tasks", status_code=201)
def create_task(
    body: CreateTaskRequest,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    registry: AccountRegistry = Depends(get_registry),
    scheduler: Scheduler = Depends(get_scheduler),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    reporter: Any = Depends(get_run_reporter),
    workflows: WorkflowService = Depends(get_workflow_service),
    resources_registry: Any = Depends(get_resources),
    capabilities: Any = Depends(get_capabilities),
    task_store: TaskStore = Depends(get_task_store),
    resolver: Any = Depends(get_repo_resolver),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Create a task: revalidate, reserve a slot, launch the first run.

    Resolution is authoritative here — a stale preflight answer is never
    trusted. The named-account lease is taken by the shared create path
    (``Scheduler.acquire``), so the reserve is atomic.
    """
    fingerprint = request_fingerprint(body)
    owned = None
    if idempotency_key:
        outcome, entry = v1.idempotency.claim(key.id, idempotency_key, fingerprint)
        if outcome == "hit":
            return entry.body
        if outcome == "conflict":
            raise V1ApiError(
                409,
                "idempotency_conflict",
                "Idempotency-Key was already used with a different request body",
            )
        if outcome == "timeout":
            raise V1ApiError(
                409,
                "idempotency_in_progress",
                "a create with this Idempotency-Key is still in progress",
            )
        # Durable bound: a replay after a control-plane restart resolves to
        # the original task via its stored idempotency pin.
        prior = task_store.find_by_idempotency(key.id, idempotency_key)
        if prior is not None:
            prior_fp = (prior.idempotency or {}).get("fingerprint")
            if prior_fp not in (None, fingerprint):
                v1.idempotency.abandon(key.id, idempotency_key, entry)
                raise V1ApiError(
                    409,
                    "idempotency_conflict",
                    "Idempotency-Key was already used with a different request body",
                )
            v1.idempotency.complete(
                key.id, idempotency_key, entry, agent_id=prior.agent_id, body=prior.response
            )
            v1.idempotency.settle(key.id, idempotency_key, entry)
            return prior.response or _task_detail(
                prior,
                plane=plane,
                v1=v1,
                run_states=run_states,
                workflows=workflows,
                scheduler=scheduler,
                reporter=reporter,
            )
        owned = entry

    on_provisioned = None
    if owned is not None:
        # Same in-flight semantics as POST /v1/agents: a duplicate that
        # lands mid-provision waits for the worker allocation rather than
        # racing a second create.
        on_provisioned = lambda: v1.idempotency.settle(  # noqa: E731
            key.id, idempotency_key, owned
        )
    try:
        result = _create_task_once(
            body,
            key,
            plane,
            registry,
            scheduler,
            v1,
            run_states,
            workflows,
            reporter=reporter,
            resources_registry=resources_registry,
            capabilities=capabilities,
            task_store=task_store,
            resolver=resolver,
            idempotency_key=idempotency_key,
            idempotency_fingerprint=fingerprint,
            on_provisioned=on_provisioned,
        )
    except Exception:
        if owned is not None:
            v1.idempotency.abandon(key.id, idempotency_key, owned)
        raise
    if owned is not None:
        v1.idempotency.complete(
            key.id,
            idempotency_key,
            owned,
            agent_id=(result.get("task") or {}).get("agent_id"),
            body=result,
        )
    return result


def _create_task_once(
    body: CreateTaskRequest,
    key: ApiKey,
    plane: Any,
    registry: AccountRegistry,
    scheduler: Scheduler,
    v1: V1State,
    run_states: RunStateStore,
    workflows: WorkflowService,
    *,
    reporter: Any,
    resources_registry: Any,
    capabilities: Any,
    task_store: TaskStore,
    resolver: Any,
    idempotency_key: str | None,
    idempotency_fingerprint: str | None,
    on_provisioned: Any = None,
) -> dict[str, Any]:
    """Resolve → validate → reserve → launch, then persist the record."""
    resolution = _resolve(
        body,
        registry=registry,
        scheduler=scheduler,
        capabilities=capabilities,
        resolver=resolver,
    )
    # git policy is already domain-validated; resolve onto the exact base.
    git = resolution.git
    workspace = resolution.source.workspace() if resolution.source is not None else None
    contract = _routes._normalize_contract(body.output_contract)
    compute = _routes._validate_compute(body)
    if contract is not None and _routes._ledger(plane) is None:
        raise V1ApiError(409, "session_not_runnable", "output contracts require the run ledger")

    eligible = [c for c in resolution.execution.candidates if c.eligible]
    if not eligible:
        raise V1ApiError(409, "account_unavailable", "no eligible account")
    # The recorded pick is authoritative: attempt it first, then fail over
    # in the same least-recently-used order — never registry listing order —
    # so the account that actually runs matches the durable resolved record.
    pick = resolution.execution.pick
    rest = sorted(
        (c for c in eligible if c is not pick),
        key=lambda c: (c.last_used_at or "", c.account_id),
    )
    candidates = ([pick] if pick is not None else []) + rest
    last_error: V1ApiError | None = None
    for candidate in candidates:
        agent = _candidate_spec(candidate)
        request = _agent_request(body, agent)
        # Resource validity is provider-scoped — validate per candidate
        # before its create attempt, not once for a guessed provider.
        resources = _routes._validate_resources(request, resources_registry)
        try:
            result = _routes._create_agent_once(
                request,
                key,
                plane,
                registry,
                scheduler,
                v1,
                run_states,
                workflows,
                capabilities=capabilities,
                reporter=reporter,
                idempotency_key=idempotency_key,
                idempotency_fingerprint=idempotency_fingerprint,
                workspace=workspace,
                git=git,
                output_contract=contract,
                resources=resources,
                compute=compute,
                reasoning_effort=candidate.effort,
                on_provisioned=on_provisioned,
            )
        except V1ApiError as exc:
            if exc.code in _RETRYABLE_SCHEDULE_CODES:
                # Drift between the advisory pick and the atomic reserve:
                # retry the next eligible candidate before giving up.
                last_error = exc
                continue
            raise
        task = TaskRecord(
            id=taskmod.new_task_id(),
            owner=key.id,
            status="queued",
            request=_spec(body),
            resolved=_resolved_for(resolution, candidate),
            agent_id=result["agent"]["id"],
            run_id=result["run"]["id"],
            created_at=taskmod._iso_now(),
            updated_at=taskmod._iso_now(),
            idempotency=(
                {"key_id": key.id, "key": idempotency_key, "fingerprint": idempotency_fingerprint}
                if idempotency_key
                else None
            ),
        )
        response = {**result, "task": task.public()}
        task.response = response
        task_store.put(task)
        return response
    assert last_error is not None
    raise last_error


@router.get("/tasks")
def list_tasks(
    key: ApiKey = Depends(agents_key),
    task_store: TaskStore = Depends(get_task_store),
    run_states: RunStateStore = Depends(get_run_states),
    plane: Any = Depends(get_plane),
) -> dict[str, Any]:
    """List the caller's tasks (created_at order)."""
    tasks = [_task_public(rec, run_states, plane) for rec in task_store.list(key.id)]
    return {"tasks": tasks}


@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    workflows: WorkflowService = Depends(get_workflow_service),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: Any = Depends(get_run_reporter),
    task_store: TaskStore = Depends(get_task_store),
) -> dict[str, Any]:
    """Task detail: requested vs resolved + live agent/run projections."""
    record = task_store.get(task_id)
    if record is None or record.owner != key.id:
        raise not_found("task not found")
    return _task_detail(
        record,
        plane=plane,
        v1=v1,
        run_states=run_states,
        workflows=workflows,
        scheduler=scheduler,
        reporter=reporter,
    )
