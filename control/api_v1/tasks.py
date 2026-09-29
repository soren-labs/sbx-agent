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
from control.run_store import TERMINAL_RUN_STATUSES
from control.service import SessionConflict
from control.tasks import TaskRecord, TaskRefusal, TaskStore
from control.workspace import record_to_dict as workspace_record_to_dict

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


# Git policy keys that make remote delivery required — a task whose run
# FINISHED but whose required delivery did not land is NOT complete
# (SOR-224): it reports ``delivery_failed`` (or ``delivering`` while the
# publish attempt is still in flight) instead of ``finished``.
_DELIVERY_KEYS = ("push", "auto_publish", "auto_create_pr", "merge")

_RUN_TO_TASK = {
    "CREATING": "queued",
    "QUEUED": "queued",
    "RUNNING": "running",
    "FINISHED": "finished",
    "ERROR": "error",
    "CANCELLED": "cancelled",
    "EXPIRED": "expired",
}

_TASK_TERMINAL = frozenset({"finished", "error", "cancelled", "expired", "delivery_failed"})


def _ws_record(plane: Any, agent_id: str | None) -> dict[str, Any] | None:
    """The agent's durable workspace record as a public dict, or None."""
    workspaces = getattr(plane, "workspaces", None)
    if workspaces is None or agent_id is None:
        return None
    try:
        record = workspaces.get(agent_id)
    except Exception:
        return None
    return workspace_record_to_dict(record) if record is not None else None


def _delivery_view(record: TaskRecord, ws: dict[str, Any] | None) -> dict[str, Any] | None:
    """Delivery sub-state: required / pending / delivered / failed.

    The resolved git policy — the one actually persisted on the workspace
    record, falling back to the task's resolved declaration — decides
    whether a remote delivery is owed. A recorded ``publish_error`` is a
    failed delivery even though the run itself kept its FINISHED verdict.
    """
    git = (ws or {}).get("git") or (record.resolved or {}).get("git")
    if not isinstance(git, dict):
        return None
    required = any(bool(git.get(k)) for k in _DELIVERY_KEYS)
    if not required:
        return None
    out: dict[str, Any] = {"required": True}
    if ws is not None:
        if ws.get("branch"):
            out["branch"] = ws["branch"]
        if ws.get("pushed_head_sha"):
            out["pushed_head_sha"] = ws["pushed_head_sha"]
        if ws.get("pull_request") is not None:
            out["pull_request"] = ws["pull_request"]
        if ws.get("merge") is not None:
            out["merge"] = ws["merge"]
        if ws.get("publish_error"):
            out["error"] = ws["publish_error"]
    # Every declared step must have landed: a push alone does not satisfy
    # an owed pull request (the durable pushed_head_sha is recorded before
    # the PR step runs, so it can coexist with a failed delivery).
    satisfied = bool(ws) and all(
        [
            (not git.get("merge")) or bool(((ws or {}).get("merge") or {}).get("merged")),
            (not git.get("auto_create_pr")) or bool((ws or {}).get("pull_request")),
            (not (git.get("push") or git.get("auto_publish")))
            or bool((ws or {}).get("pushed_head_sha")),
        ]
    )
    if ws is not None and ws.get("publish_error") and not satisfied:
        out["status"] = "failed"
    elif satisfied:
        out["status"] = "delivered"
    else:
        out["status"] = "pending"
    return out


def _revision_view(ws: dict[str, Any] | None) -> dict[str, Any] | None:
    """Revision sub-state: is there materialized work to deliver?"""
    if ws is None:
        return None
    head = ws.get("head_sha")
    base = ws.get("base_sha")
    return {
        "head_sha": head,
        "base_sha": base,
        "status": ("none" if head is None else ("unchanged" if head == base else "ready")),
    }


def _run_statuses(
    record: TaskRecord, run_states: RunStateStore, plane: Any
) -> list[tuple[int, str]]:
    """``(n, run status)`` for every run of the task's agent, ledger-first."""
    if record.agent_id is None:
        return []
    ledger = getattr(plane, "run_ledger", None)
    if ledger is not None:
        try:
            return sorted((r.n, r.status) for r in ledger.list(record.agent_id))
        except Exception:
            pass
    state = run_states.get(record.agent_id, 1)
    return [(1, state.status)] if state is not None else []


def _aggregate_status(
    record: TaskRecord,
    run_states: RunStateStore,
    plane: Any,
    ws: dict[str, Any] | None,
    *,
    prefetched: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Live ``(task status, machine-readable reason)``.

    Aggregation order (SOR-224): active work first — any RUNNING run means
    ``running``, any parked QUEUED/CREATING run means ``queued``; an
    explicit task cancel is sticky while nothing is active; otherwise the
    latest terminal run verdict rules, and a FINISHED run still owes its
    required delivery before the task may report ``finished``.
    """
    if record.agent_id is None:
        return record.status, "awaiting_dispatch"
    prefetched = prefetched or {}
    rec = prefetched["rec"] if "rec" in prefetched else plane.get(record.agent_id)
    if rec is not None and rec.status == "running":
        # SOR-268: settle-on-read is throttled per session — the full
        # reconcile costs poll + turns/<n>.json per call, which under
        # fanout/SSE load is the starvation vector; mutation paths still
        # run the unthrottled ``reconcile_turn``.
        reconcile = getattr(plane, "maybe_reconcile_turn", None) or getattr(
            plane, "reconcile_turn", None
        )
        if callable(reconcile):
            reconcile(record.agent_id)
            rec = plane.get(record.agent_id) or rec
    statuses = (
        prefetched["statuses"]
        if "statuses" in prefetched
        else _run_statuses(record, run_states, plane)
    )
    if any(s == "RUNNING" for _, s in statuses):
        return "running", "run_active"
    if any(s in ("QUEUED", "CREATING") for _, s in statuses):
        return "queued", "queued_work"
    if record.status == "cancelled":
        return "cancelled", "task_cancelled"
    terminal = [s for _, s in statuses if s in TERMINAL_RUN_STATUSES]
    if terminal:
        latest = terminal[-1]
        if latest == "FINISHED":
            delivery = _delivery_view(record, ws)
            if delivery is not None:
                if delivery["status"] == "failed":
                    return "delivery_failed", "delivery_failed"
                if delivery["status"] != "delivered":
                    return "delivering", "delivery_pending"
            return "finished", "run_finished"
        return _RUN_TO_TASK[latest], {
            "ERROR": "run_error",
            "CANCELLED": "run_cancelled",
            "EXPIRED": "run_expired",
        }[latest]
    if rec is not None and rec.status in ("closed", "timed_out", "lost"):
        return {"closed": "cancelled", "timed_out": "expired", "lost": "error"}[
            rec.status
        ], f"session_{rec.status}"
    return (
        ("queued", "awaiting_dispatch")
        if record.status == "queued"
        else (
            record.status,
            "stored",
        )
    )


def _settle_task(
    record: TaskRecord,
    task_store: TaskStore,
    plane: Any,
    run_states: RunStateStore,
) -> tuple[str, dict[str, Any] | None]:
    """Converge the durable record to the live aggregate, logging changes.

    Same read-path convergence as the run ledger backfill: every read or
    mutation recomputes the aggregate, appends a ``{status, reason, at}``
    transition on change, and persists — so the durable record is truthful
    for orchestration even across restarts.
    """
    ws = _ws_record(plane, record.agent_id)
    status, reason = _aggregate_status(record, run_states, plane, ws)
    if status != record.status:
        record.transitions.append({"status": status, "reason": reason, "at": taskmod._iso_now()})
        record.status = status
        record.updated_at = taskmod._iso_now()
        try:
            task_store.put(record)
        except Exception:
            # The live aggregate is the answer either way — a failed
            # convergence write must not 500 a status read.
            pass
    return status, ws


def _task_public(
    record: TaskRecord,
    run_states: RunStateStore,
    plane: Any,
    task_store: TaskStore,
    *,
    ws: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = record.public()
    if ws is None:
        status, ws = _settle_task(record, task_store, plane, run_states)
    else:
        status = record.status
    out["status"] = status
    out["prompt"] = (record.request or {}).get("prompt")
    delivery = _delivery_view(record, ws)
    if delivery is not None:
        out["delivery"] = delivery
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
    task_store: TaskStore,
) -> dict[str, Any]:
    """Task record + live agent/run projections (same shapes as /v1/agents)."""
    status, ws = _settle_task(record, task_store, plane, run_states)
    task = _task_public(record, run_states, plane, task_store, ws=ws)
    task["status"] = status
    revision = _revision_view(ws)
    if revision is not None:
        task["revision"] = revision
    out: dict[str, Any] = {"task": task}
    if record.agent_id is None:
        out["agent"] = None
        out["run"] = None
        out["runs"] = []
        return out
    rec = plane.get(record.agent_id)
    if rec is None:
        out["agent"] = None
        out["run"] = None
        out["runs"] = []
        return out
    pub = plane.public(rec)
    meta = _routes._meta_for(v1, rec)
    out["agent"] = _routes._agent_payload(plane, v1, workflows, rec)
    out["run"] = _routes._run_public(
        plane,
        pub,
        rec,
        rec.current_turn_n or 1,
        v1.cancelled(rec.id),
        meta,
        run_states,
        scheduler=scheduler,
        reporter=reporter,
    )
    out["runs"] = _task_runs(plane, rec, v1, run_states, scheduler, reporter)
    return out


def _task_runs(
    plane: Any,
    rec: Any,
    v1: V1State,
    run_states: RunStateStore,
    scheduler: Any,
    reporter: Any,
    *,
    rows: Any = None,
) -> list[dict[str, Any]]:
    """Every run of the task's agent; queued runs carry ``queue_position``.

    ``rows`` may carry the caller's prefetched ``ledger.list`` result: the
    statuses and the per-run ``record`` prefetch then ride the same single
    list call, so a detail render costs O(1) remote reads instead of one
    serial ``ledger.get`` per run.
    """
    ledger = getattr(plane, "run_ledger", None)
    if rows is None and ledger is not None:
        try:
            rows = ledger.list(rec.id)
        except Exception:
            rows = None
    by_n: dict[int, Any] = {}
    if rows is not None:
        statuses = dict(sorted((r.n, r.status) for r in rows))
        by_n = {r.n: r for r in rows}
    else:
        statuses = dict(_run_statuses_for(rec.id, run_states, plane))
    queue = [n for n, s in sorted(statuses.items()) if s == "QUEUED"]
    pub = plane.public(rec)
    meta = _routes._meta_for(v1, rec)
    cancelled = v1.cancelled(rec.id)
    runs: list[dict[str, Any]] = []
    for n in sorted(statuses):
        prefetch = {"record": by_n.get(n)} if rows is not None else {}
        view = _routes._run_public(
            plane,
            pub,
            rec,
            n,
            cancelled,
            meta,
            run_states,
            scheduler=scheduler,
            reporter=reporter,
            **prefetch,
        )
        if statuses[n] == "QUEUED":
            view["queue_position"] = queue.index(n) + 1
        runs.append(view)
    return runs


def _run_statuses_for(
    agent_id: str, run_states: RunStateStore, plane: Any
) -> list[tuple[int, str]]:
    ledger = getattr(plane, "run_ledger", None)
    if ledger is not None:
        try:
            return sorted((r.n, r.status) for r in ledger.list(agent_id))
        except Exception:
            pass
    state = run_states.get(agent_id, 1)
    return [(1, state.status)] if state is not None else []


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
                task_store=task_store,
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
    new_id: Any = None,
) -> dict[str, Any]:
    """Resolve → validate → reserve → launch, then persist the record.

    ``new_id`` mints the record id (default ``task_…``); the V2 session
    facade passes its own ``sess_…`` minter so sessions and tasks share
    the durable store without sharing the id namespace.
    """
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
            id=(new_id or taskmod.new_task_id)(),
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
    tasks = [_task_public(rec, run_states, plane, task_store) for rec in task_store.list(key.id)]
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
        task_store=task_store,
    )


# ---------------------------------------------------------------------------
# SOR-224: run queue / cancel / retry / delivery
# ---------------------------------------------------------------------------


class CreateTaskRunRequest(BaseModel):
    """``POST /v1/tasks/{id}/runs`` body — a follow-up turn on the task's agent."""

    model_config = ConfigDict(extra="forbid")

    prompt: Prompt
    on_busy: Literal["queue", "reject"] = "queue"
    metadata: WorkflowMetadata | None = None
    output_contract: OutputContract | None = None


class RetryTaskRequest(BaseModel):
    """``POST /v1/tasks/{id}/retry`` body.

    ``mode=None`` picks by task state: ``delivery_failed`` retries the
    publish; a terminal run verdict re-runs the task's original prompt
    (or ``prompt`` when overridden). ``on_busy`` applies to run-mode.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["delivery", "run"] | None = None
    prompt: Prompt | None = None
    on_busy: Literal["queue", "reject"] = "queue"


def _require_task(task_store: TaskStore, key: ApiKey, task_id: str) -> TaskRecord:
    record = task_store.get(task_id)
    if record is None or record.owner != key.id:
        raise not_found("task not found")
    return record


def _require_task_agent(record: TaskRecord, plane: Any) -> Any:
    if record.agent_id is None:
        raise V1ApiError(409, "session_not_runnable", "task has no agent yet")
    rec = plane.get(record.agent_id)
    if rec is None:
        raise V1ApiError(409, "session_not_runnable", "task's agent is gone")
    return rec


def _publish_delivery(plane: Any, agent_id: str) -> None:
    """Re-execute the recorded git policy on the agent's live sandbox.

    Push/PR publish first; when the policy also declares ``merge``, the
    merge step follows — the same order the /v1 git endpoints use.
    """
    recover = getattr(plane, "recover_session", None)
    if callable(recover):
        recover(agent_id)
    rec = plane.get(agent_id)
    handle = rec.handle() if rec is not None else None
    if handle is None:
        raise V1ApiError(409, "session_not_runnable", "agent has no live sandbox")
    workspaces = getattr(plane, "workspaces", None)
    if workspaces is None:
        raise V1ApiError(409, "workspace_unavailable", "workspace service unavailable")
    try:
        record = workspaces.publish(handle, agent_id)
        if (record.git or {}).get("merge") and not (record.merge or {}).get("merged"):
            workspaces.merge(handle, agent_id)
    except Exception as exc:
        raise _routes._workspace_error(exc) from exc


@router.post("/tasks/{task_id}/runs", status_code=201)
def create_task_run(
    task_id: str,
    body: CreateTaskRunRequest,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: Any = Depends(get_run_reporter),
    workflows: WorkflowService = Depends(get_workflow_service),
    task_store: TaskStore = Depends(get_task_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Queue a follow-up run on the task's agent (durable QUEUED by default).

    Same idempotency semantics as run/agent/task create: same key + same
    body replays the original run, same key + different body conflicts —
    the pin is durable on the run's own ledger record, so a replay after a
    control-plane restart still resolves.
    """
    record = _require_task(task_store, key, task_id)
    rec = _require_task_agent(record, plane)
    agent_id = record.agent_id
    contract = _routes._normalize_contract(body.output_contract)
    if contract is not None and _routes._ledger(plane) is None:
        raise V1ApiError(409, "session_not_runnable", "output contracts require the run ledger")
    fingerprint = request_fingerprint(body)
    pin_key = f"task:{task_id}:run:{idempotency_key}" if idempotency_key else None
    owned = None
    pin = None
    if idempotency_key:
        outcome, entry = v1.idempotency.claim(key.id, pin_key or "", fingerprint)
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
        ledger = _routes._ledger(plane)
        if ledger is not None:
            prior = ledger.find_by_idempotency(agent_id, key.id, pin_key or "")
            if prior is not None:
                prior_fp = (prior.idempotency or {}).get("fingerprint")
                if prior_fp not in (None, fingerprint):
                    v1.idempotency.abandon(key.id, pin_key or "", entry)
                    raise V1ApiError(
                        409,
                        "idempotency_conflict",
                        "Idempotency-Key was already used with a different request body",
                    )
                run_view = _routes._run_public(
                    plane,
                    plane.public(rec),
                    rec,
                    prior.n,
                    v1.cancelled(agent_id),
                    _routes._meta_for(v1, rec),
                    run_states,
                    scheduler=scheduler,
                    reporter=reporter,
                )
                result = {
                    "task": _task_public(record, run_states, plane, task_store),
                    "run": run_view,
                }
                v1.idempotency.complete(
                    key.id, pin_key or "", entry, agent_id=agent_id, body=result
                )
                v1.idempotency.settle(key.id, pin_key or "", entry)
                return result
        owned = entry
        pin = {"key_id": key.id, "key": pin_key, "fingerprint": fingerprint}
    try:
        turn_id = plane.post_message(
            agent_id,
            body.prompt.text,
            output_contract=contract,
            queue=body.on_busy == "queue",
            idempotency=pin,
        )
    except KeyError:
        if owned is not None:
            v1.idempotency.abandon(key.id, pin_key or "", owned)
        raise not_found("agent not found") from None
    except SessionConflict as exc:
        if owned is not None:
            v1.idempotency.abandon(key.id, pin_key or "", owned)
        raise V1ApiError(
            exc.code,
            exc.error,
            exc.error,
            retry_after=(plane.turn_max_seconds if exc.error == "turn_in_progress" else None),
        ) from exc
    if body.metadata is not None:
        workflows.attach(owner=key.id, agent_id=agent_id, metadata=body.metadata)
    n = int(turn_id.rsplit("-", 1)[-1])
    rec = _require_task_agent(record, plane)
    run_view = _routes._run_public(
        plane,
        plane.public(rec),
        rec,
        n,
        v1.cancelled(agent_id),
        _routes._meta_for(v1, rec),
        run_states,
        scheduler=scheduler,
        reporter=reporter,
    )
    result = {"task": _task_public(record, run_states, plane, task_store), "run": run_view}
    if owned is not None:
        v1.idempotency.complete(key.id, pin_key or "", owned, agent_id=agent_id, body=result)
        v1.idempotency.settle(key.id, pin_key or "", owned)
    return result


@router.get("/tasks/{task_id}/runs")
def list_task_runs(
    task_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: Any = Depends(get_run_reporter),
    task_store: TaskStore = Depends(get_task_store),
) -> dict[str, Any]:
    """Every run the task has allocated — QUEUED records carry ``queue_position``."""
    record = _require_task(task_store, key, task_id)
    rec = plane.get(record.agent_id) if record.agent_id else None
    runs = _task_runs(plane, rec, v1, run_states, scheduler, reporter) if rec is not None else []
    return {"task": _task_public(record, run_states, plane, task_store), "runs": runs}


@router.post("/tasks/{task_id}/cancel")
def cancel_task(
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
    """Cancel the task's outstanding work — idempotent.

    QUEUED runs are cancelled in place (they never dispatch); a still-
    provisioning run-1 is dropped pre-dispatch; a RUNNING turn is stopped.
    An already-cancelled (or fully terminal) task replays its state.
    """
    record = _require_task(task_store, key, task_id)
    status, _ = _settle_task(record, task_store, plane, run_states)
    if status == "cancelled" or record.agent_id is None:
        if status != "cancelled":
            # Nothing ever dispatched — the durable cancel is the task's.
            record.transitions.append(
                {"status": "cancelled", "reason": "task_cancelled", "at": taskmod._iso_now()}
            )
            record.status = "cancelled"
            record.updated_at = taskmod._iso_now()
            task_store.put(record)
        return _task_detail(
            record,
            plane=plane,
            v1=v1,
            run_states=run_states,
            workflows=workflows,
            scheduler=scheduler,
            reporter=reporter,
            task_store=task_store,
        )
    if status in _TASK_TERMINAL:
        # Terminal tasks have nothing left to cancel — replay, no 409.
        return _task_detail(
            record,
            plane=plane,
            v1=v1,
            run_states=run_states,
            workflows=workflows,
            scheduler=scheduler,
            reporter=reporter,
            task_store=task_store,
        )
    agent_id = record.agent_id
    ledger = _routes._ledger(plane)
    queued = [n for n, s in _run_statuses_for(agent_id, run_states, plane) if s == "QUEUED"]
    for n in queued:
        if ledger is not None:
            ledger.cancel(agent_id, n)
        else:
            run_states.transition(agent_id, n, "CANCELLED")
        v1.mark_cancelled(agent_id, n)
    rec = plane.get(agent_id)
    if rec is not None and rec.status == "creating":
        plane.discard_queued_first_turn(agent_id)
        run_states.transition(agent_id, 1, "CANCELLED")
        v1.mark_cancelled(agent_id, 1)
        rec = plane.get(agent_id) or rec
    if rec is not None and rec.status == "running":
        try:
            plane.stop(agent_id)
        except KeyError:
            pass
    record.transitions.append(
        {"status": "cancelled", "reason": "task_cancelled", "at": taskmod._iso_now()}
    )
    record.status = "cancelled"
    record.updated_at = taskmod._iso_now()
    task_store.put(record)
    return _task_detail(
        record,
        plane=plane,
        v1=v1,
        run_states=run_states,
        workflows=workflows,
        scheduler=scheduler,
        reporter=reporter,
        task_store=task_store,
    )


@router.post("/tasks/{task_id}/retry")
def retry_task(
    task_id: str,
    body: RetryTaskRequest | None = None,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    workflows: WorkflowService = Depends(get_workflow_service),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: Any = Depends(get_run_reporter),
    task_store: TaskStore = Depends(get_task_store),
) -> dict[str, Any]:
    """Retry the failed step of a terminal task.

    ``delivery_failed`` re-executes the recorded git policy (push/PR —
    ``publish`` itself is create-or-update idempotent); a terminal run
    verdict allocates a new run on the same agent. An active task is a
    ``task_active`` 409 carrying ``retry_after``.
    """
    record = _require_task(task_store, key, task_id)
    status, ws = _settle_task(record, task_store, plane, run_states)
    body = body or RetryTaskRequest()
    mode = body.mode
    if mode is None:
        mode = "delivery" if status == "delivery_failed" else "run"
    if mode == "delivery":
        delivery = _delivery_view(record, ws)
        if delivery is None:
            raise V1ApiError(409, "task_not_retryable", "task has no delivery policy")
        if delivery["status"] == "delivered":
            return _task_detail(
                record,
                plane=plane,
                v1=v1,
                run_states=run_states,
                workflows=workflows,
                scheduler=scheduler,
                reporter=reporter,
                task_store=task_store,
            )
        _require_task_agent(record, plane)
        _publish_delivery(plane, record.agent_id)
        _settle_task(record, task_store, plane, run_states)
        return _task_detail(
            record,
            plane=plane,
            v1=v1,
            run_states=run_states,
            workflows=workflows,
            scheduler=scheduler,
            reporter=reporter,
            task_store=task_store,
        )
    # mode == "run": re-run the prompt on the same agent.
    if status in ("running", "queued", "delivering"):
        raise V1ApiError(
            409,
            "task_active",
            "task still has active work",
            retry_after=getattr(plane, "turn_max_seconds", None),
        )
    rec = _require_task_agent(record, plane)
    text = (
        body.prompt.text
        if body.prompt is not None
        else ((record.request or {}).get("prompt") or {}).get("text")
    )
    if not text:
        raise V1ApiError(409, "task_not_retryable", "task has no prompt to retry")
    try:
        turn_id = plane.post_message(record.agent_id, text, queue=body.on_busy == "queue")
    except SessionConflict as exc:
        raise V1ApiError(
            exc.code,
            exc.error,
            exc.error,
            retry_after=(plane.turn_max_seconds if exc.error == "turn_in_progress" else None),
        ) from exc
    n = int(turn_id.rsplit("-", 1)[-1])
    rec = _require_task_agent(record, plane)
    run_view = _routes._run_public(
        plane,
        plane.public(rec),
        rec,
        n,
        v1.cancelled(rec.id),
        _routes._meta_for(v1, rec),
        run_states,
        scheduler=scheduler,
        reporter=reporter,
    )
    _settle_task(record, task_store, plane, run_states)
    detail = _task_detail(
        record,
        plane=plane,
        v1=v1,
        run_states=run_states,
        workflows=workflows,
        scheduler=scheduler,
        reporter=reporter,
        task_store=task_store,
    )
    detail["run"] = run_view
    return detail


@router.post("/tasks/{task_id}/delivery")
def deliver_task(
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
    """Execute the task's delivery policy now — a manual publish trigger.

    ``publish`` is create-or-update idempotent: replaying an already-
    delivered task returns its state; a missing delivery policy is a
    ``task_not_retryable`` 409.
    """
    record = _require_task(task_store, key, task_id)
    _, ws = _settle_task(record, task_store, plane, run_states)
    delivery = _delivery_view(record, ws)
    if delivery is None:
        raise V1ApiError(409, "task_not_retryable", "task has no delivery policy")
    if delivery["status"] != "delivered":
        _require_task_agent(record, plane)
        _publish_delivery(plane, record.agent_id)
        _settle_task(record, task_store, plane, run_states)
    return _task_detail(
        record,
        plane=plane,
        v1=v1,
        run_states=run_states,
        workflows=workflows,
        scheduler=scheduler,
        reporter=reporter,
        task_store=task_store,
    )
