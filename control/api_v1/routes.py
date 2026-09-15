"""Public ``/v1`` endpoints (Cursor Cloud Agents shape, ``api-v1.yaml``).

``agent ≙ session``, ``run ≙ turn``: ``POST /v1/agents`` creates a session and
immediately queues its first run; follow-ups are new runs on the same agent.
All endpoints consume ``ports.*`` Protocols plus the shared SessionService
(``app.state.plane``); provider / account metadata is tracked in ``V1State``
until P2-C persists it on the session record.
"""

from __future__ import annotations

import asyncio
import json
import queue
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Header, Request
from fastapi.responses import Response

from control.api_v1 import router
from control.api_v1.bootstrap import PROVIDER_DEFAULT_MODELS
from control.api_v1.deps import (
    RunFailureReporter,
    admin_key,
    agents_key,
    api_key,
    get_artifact_store,
    get_handoffs,
    get_key_store,
    get_plane,
    get_registry,
    get_run_reporter,
    get_run_states,
    get_scheduler,
    get_v1_state,
    get_workflow_service,
    get_workspaces,
)
from control.api_v1.errors import V1ApiError, not_found
from control.api_v1.lifecycle import (
    RUN_TERMINAL,
    RunStateStore,
    launch_first_run,
    request_fingerprint,
)
from control.api_v1.schemas import (
    VALID_SCOPES,
    CreateAccountRequest,
    CreateAgentRequest,
    CreateApiKeyRequest,
    CreateArtifactRequest,
    CreateRunRequest,
    HandoffRef,
    ProviderId,
    ReviewWorkspaceRequest,
    account_public,
    agent_public,
    api_key_public,
    usage_public,
)
from control.api_v1.state import AgentMeta, V1State
from control.api_v1.workflows import WorkflowService
from control.artifact_ops import credential_forbidden_values, snapshot_workspace_artifact
from control.artifacts import (
    ArtifactCorruptError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactSecretError,
    manifest_to_dict,
)
from control.config import TERMINAL_STATUSES
from control.devin_pool import ScheduleRefused
from control.ports import Account, AccountRegistry, ApiKey, ApiKeyStore, Scheduler
from control.run_errors import run_error_for_run
from control.run_store import (
    UNKNOWN_RUN_STATUS,
    RunRecord,
    default_artifact_refs,
    outcome_from_turn_payload,
)
from control.sandbox_io import read_json, read_text, sandbox_env
from control.service import ConcurrencyLimit, SessionConflict, format_sse
from control.workspace import (
    ARTIFACT_NOT_FOUND,
    WORKSPACE_INVALID,
    WORKSPACE_NOT_FOUND,
    WorkspaceError,
    WorkspaceSpec,
)
from control.workspace import record_to_dict as workspace_record_to_dict

AGENTS_PAGE_SIZE = 100
_TURN_ID_RE = re.compile(r"^turn-(\d+)$")
_RUN_ID_RE = re.compile(r"^run-(\d+)$")
_VERIFY_TAG = "account-verify"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _turn_n(turn_id: str | None) -> int | None:
    match = _TURN_ID_RE.match(turn_id or "")
    return int(match.group(1)) if match else None


def _run_n(run_id: str) -> int | None:
    match = _RUN_ID_RE.match(run_id or "")
    return int(match.group(1)) if match else None


def _ledger(plane: Any) -> Any:
    """The durable run ledger attached to the plane (None when absent)."""
    return getattr(plane, "run_ledger", None)


def _meta_for(v1: V1State, rec: Any) -> AgentMeta:
    """Agent metadata with a restart-stable fallback to sandbox tags.

    ``V1State`` is per-process; after a control-plane restart the durable
    session record's sandbox tags still carry provider/account, so identity
    does not drift back to defaults (SOR-82).
    """
    meta = v1.get_meta(rec.id)
    if meta is not None:
        return meta
    tags = getattr(rec, "sandbox_tags", None) or {}
    return AgentMeta(
        provider=tags.get("provider") or "codex",
        account_id=tags.get("account_id") or "auto",
    )


def _known_run_ns(rec: Any, ledger: Any = None, run_states: Any = None) -> set[int]:
    ns = set(range(1, int(rec.turns) + 1))
    for message in rec.messages:
        n = _turn_n(message.get("turn_id"))
        if n is not None:
            ns.add(n)
    if rec.current_turn_n is not None:
        ns.add(int(rec.current_turn_n))
    if ledger is not None:
        # The ledger is authoritative: a run persisted there is known even
        # when the session record lost the matching messages/turn count.
        try:
            ns.update(record.n for record in ledger.list(rec.id))
        except Exception:
            pass
    if run_states is not None:
        # A separate run-state seam (ledger-less deployments, injected test
        # stores) can know runs the ledger does not — e.g. a queued CREATING
        # run-1 before the ledger saw it.
        try:
            ns.update(s.n for s in run_states.list(rec.id))
        except Exception:
            pass
    return ns


def _turn_payload(plane: Any, rec: Any, n: int) -> dict[str, Any] | None:
    """Read ``turns/<n>.json`` when the sandbox is still reachable."""
    backend = getattr(plane, "backend", None)
    handle = rec.handle()
    if backend is None or handle is None:
        return None
    try:
        poll = backend.poll(handle)
    except Exception:
        return None
    if not poll.alive:
        return None
    try:
        payload = read_json(backend, handle, f"turns/{n}.json")
    except Exception:
        return None
    return payload


def _record_public(
    record: RunRecord,
    pub: dict[str, Any],
    meta: Any = None,
    *,
    status: str | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persisted ``RunRecord`` → Cursor-shaped Run (authoritative)."""
    run: dict[str, Any] = {
        "id": record.id,
        "agent_id": record.agent_id,
        "status": status or record.status,
        "created_at": record.created_at or pub["created_at"],
        "updated_at": record.updated_at or pub["updated_at"],
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "result": {"text": record.result_text} if record.result_text else None,
        "error": record.error if error is None else error,
        "provider": record.provider or (meta.provider if meta else None),
        "account_id": record.account_id or (meta.account_id if meta else None),
        "model": record.model or pub.get("model"),
        "artifact_refs": list(record.artifact_refs),
    }
    if record.usage is not None:
        run["usage"] = usage_public(record.usage)
    return run


def _fallback_status(rec: Any) -> str:
    """Honest status for a run whose evidence is gone, keyed on the session.

    ``closed`` means the agent was deleted (run cancelled); ``timed_out``
    means the sandbox expired mid-flight; anything else — including
    ``lost`` and sessions that are still live but no longer own the turn —
    is explicit ``UNKNOWN``, never inferred success.
    """
    if rec.status == "closed":
        return "CANCELLED"
    if rec.status == "timed_out":
        return "EXPIRED"
    return "UNKNOWN"


def _run_error_public(
    status: str,
    *,
    payload: Any = None,
    cancelled: bool = False,
    agent_status: str | None = None,
) -> dict[str, Any] | None:
    """Canonical ``run.error`` payload for a derived (non-ledger) status."""
    err = run_error_for_run(
        status,
        payload=payload,
        cancelled=cancelled,
        agent_status=agent_status,
    )
    return err.public() if err is not None else None


def _run_public(
    plane: Any,
    pub: dict[str, Any],
    rec: Any,
    n: int,
    cancelled: set[int],
    meta: Any = None,
    run_states: RunStateStore | None = None,
    *,
    scheduler: Any = None,
    reporter: Any = None,
) -> dict[str, Any]:
    """Render the run, then feed terminal provider errors to the scheduler.

    Reporting is the /v1 cooldown/failover seam (SOR-63/D2): a rendered
    terminal provider error (``rate_limited``, ``auth_invalid``, …) marks
    the run's account via ``RunFailureReporter``, deduped per run. Both
    knobs default off so every existing call site keeps its shape.
    """
    run = _render_run(plane, pub, rec, n, cancelled, meta, run_states)
    if reporter is not None and scheduler is not None:
        reporter.report(
            scheduler=scheduler,
            agent_id=rec.id,
            n=n,
            account_id=run.get("account_id"),
            status=run.get("status"),
            error=run.get("error"),
        )
    return run


def _render_run(
    plane: Any,
    pub: dict[str, Any],
    rec: Any,
    n: int,
    cancelled: set[int],
    meta: Any = None,
    run_states: RunStateStore | None = None,
) -> dict[str, Any]:
    """Cursor-shaped Run; the durable ledger is authoritative once written.

    Open records and pre-ledger sessions fall back to evidence-checked
    derivation: a readable ``turns/<n>.json`` decides the terminal status
    (persisted into the ledger when one is attached); without evidence the
    status is explicit ``UNKNOWN`` or the session-derived fallback — never
    inferred ``FINISHED``. While the session is still live, a persisted open
    record's own status (``CREATING`` pre-dispatch, ``RUNNING`` after) is
    authoritative — the derived view cannot see the queued-run window
    (SOR-82 A2). A separate ``RunStateStore`` (when the ledger is absent or a
    test injects one) overlays the derived view the same way.
    """
    ledger = _ledger(plane)
    record = ledger.get(rec.id, n) if ledger is not None else None
    live = rec.current_turn_n == n and rec.status not in TERMINAL_STATUSES
    if record is not None:
        if record.terminal or live:
            return _record_public(record, pub, meta)
        if n in cancelled:
            return _record_public(
                record,
                pub,
                meta,
                status="CANCELLED",
                error=_run_error_public("CANCELLED", cancelled=True, agent_status=rec.status),
            )
        payload = _turn_payload(plane, rec, n)
        if payload is not None:
            status, error, result_text, usage = outcome_from_turn_payload(payload)
            record = ledger.finish(
                rec.id,
                n,
                status=status,
                result_text=result_text,
                error=error,
                usage=usage,
                provider=meta.provider if meta else None,
                account_id=meta.account_id if meta else None,
                model=pub.get("model"),
            )
            return _record_public(record, pub, meta)
        if record.status == UNKNOWN_RUN_STATUS:
            # Corrupt stored payload: report it as-is (UNKNOWN) rather than
            # guessing a terminal state from the session.
            return _record_public(record, pub, meta)
        if rec.status in TERMINAL_STATUSES:
            # The session died with the run still open and no evidence left:
            # fall back to the session-derived terminal status.
            status = _fallback_status(rec)
            return _record_public(
                record,
                pub,
                meta,
                status=status,
                error=record.error or _run_error_public(status, agent_status=rec.status),
            )
        # Live session, open record, no readable evidence yet — the record's
        # persisted open status (CREATING/RUNNING) is the truth.
        return _record_public(record, pub, meta)

    # No ledger record (pre-ledger session or lost store): derive honestly.
    turn_id = f"turn-{n}"
    created_at: str = pub["created_at"]
    updated_at: str = pub["updated_at"]
    result_text: str | None = None
    for message in rec.messages:
        if message.get("turn_id") != turn_id:
            continue
        if message.get("role") == "user":
            created_at = str(message.get("ts") or created_at)
        elif message.get("role") == "assistant":
            result_text = str(message.get("text") or "") or None
            updated_at = str(message.get("ts") or updated_at)

    usage: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    if live:
        status = "RUNNING"
    elif n in cancelled:
        status = "CANCELLED"
    elif n <= int(rec.turns):
        payload = _turn_payload(plane, rec, n)
        if payload is not None:
            status, error, payload_text, usage = outcome_from_turn_payload(payload)
            if payload_text is not None:
                result_text = payload_text
            if ledger is not None:
                # Backfill: persist the outcome so later reads stay truthful
                # after the sandbox is reclaimed.
                record = ledger.finish(
                    rec.id,
                    n,
                    status=status,
                    result_text=result_text,
                    error=error,
                    usage=usage,
                    provider=meta.provider if meta else None,
                    account_id=meta.account_id if meta else None,
                    model=pub.get("model"),
                    created_at=created_at,
                )
                return _record_public(record, pub, meta)
        else:
            # Turn counted but outcome unreadable — explicit unknown.
            status = "UNKNOWN"
    elif rec.status in ("timed_out", "lost"):
        status = "EXPIRED"
    elif rec.status == "closed":
        status = "CANCELLED"
    else:
        # The turn ended without a turns/<n>.json record (runner-internal
        # failure) — report a diagnosable ERROR, never a silent success.
        status = "ERROR"

    if run_states is not None:
        state = run_states.get(rec.id, n)
        if state is not None:
            if state.status in RUN_TERMINAL:
                status = state.status
                updated_at = state.updated_at
            else:
                # The derived view cannot see a queued/pre-dispatch run; its
                # catch-all "CANCELLED"/"ERROR" is only real when the cancel
                # was recorded or the session went terminal.
                catch_all_terminal = (
                    status in RUN_TERMINAL
                    and n not in cancelled
                    and rec.status not in ("closed", "timed_out", "lost")
                )
                if status in RUN_TERMINAL and not catch_all_terminal:
                    # Derived terminal truth (turn finished, session died):
                    # fold it into a RUNNING record so the store converges.
                    # A CREATING record belongs to the worker, which persists
                    # ERROR/CANCELLED itself.
                    if state.status == "RUNNING":
                        run_states.transition(rec.id, n, status)
                elif status != "RUNNING":
                    status = state.status
            run_state = state
        else:
            run_state = None
    else:
        run_state = None

    error = _run_error_public(
        status,
        payload=payload,
        cancelled=n in cancelled,
        agent_status=rec.status,
    )
    run: dict[str, Any] = {
        "id": f"run-{n}",
        "agent_id": rec.id,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
        "started_at": created_at if status == "RUNNING" else None,
        "finished_at": updated_at if status != "RUNNING" else None,
        "result": {"text": result_text} if result_text else None,
        "error": error,
        "provider": meta.provider if meta else None,
        "account_id": meta.account_id if meta else None,
        "model": pub.get("model"),
        "artifact_refs": default_artifact_refs(n),
    }
    if run_state is not None and run_state.error is not None:
        run["error"] = run_state.error
    if usage is not None:
        run["usage"] = usage_public(usage)
    return run


def _runs(
    plane: Any,
    rec: Any,
    v1: V1State,
    run_states: RunStateStore | None = None,
    *,
    scheduler: Any = None,
    reporter: Any = None,
) -> list[dict[str, Any]]:
    pub = plane.public(rec)
    cancelled = v1.cancelled(rec.id)
    meta = _meta_for(v1, rec)
    return [
        _run_public(
            plane,
            pub,
            rec,
            n,
            cancelled,
            meta,
            run_states,
            scheduler=scheduler,
            reporter=reporter,
        )
        for n in sorted(_known_run_ns(rec, _ledger(plane), run_states))
    ]


def _require_agent(plane: Any, agent_id: str) -> Any:
    rec = plane.get(agent_id)
    if rec is None:
        raise not_found("agent not found")
    return rec


def _agent_payload(plane: Any, v1: V1State, workflows: WorkflowService, rec: Any) -> dict[str, Any]:
    """Agent view: contract fields + workflow binding + honest usage.

    ``usage`` comes from the session record — ``None`` (never measured)
    serializes as ``null``, never fabricated zeros (SOR-84). ``metadata``
    echoes the durable workflow/task binding when one is attached.
    """
    try:
        task = workflows.for_agent(rec.id)
    except Exception:
        task = None
    metadata = (
        {
            "workflow_id": task.workflow_id,
            "task_id": task.task_id,
            "role": task.role,
            "parent_task_id": task.parent_task_id,
        }
        if task is not None
        else None
    )
    return agent_public(plane.public(rec), _meta_for(v1, rec), usage=rec.usage, metadata=metadata)


def _require_run(
    plane: Any,
    rec: Any,
    run_id: str,
    v1: V1State,
    run_states: RunStateStore | None = None,
    *,
    scheduler: Any = None,
    reporter: Any = None,
) -> dict[str, Any]:
    n = _run_n(run_id)
    known = _known_run_ns(rec, _ledger(plane), run_states)
    if n is None or n not in known:
        raise not_found("run not found")
    pub = plane.public(rec)
    return _run_public(
        plane,
        pub,
        rec,
        n,
        v1.cancelled(rec.id),
        _meta_for(v1, rec),
        run_states,
        scheduler=scheduler,
        reporter=reporter,
    )


# ---------------------------------------------------------------- agents


def _raise_schedule_error(
    error: str | None,
    *,
    retry_after: float | None,
    provider: str,
    requested: str,
) -> None:
    if not error:
        return
    if error == "invalid_provider":
        raise V1ApiError(400, "invalid_provider", f"unknown provider {provider!r}")
    if error in ("account_busy", "account_unavailable"):
        raise V1ApiError(409, error, f"account {requested!r} cannot take the run")
    if error == "concurrency_limit":
        # Global cap (SBX_MAX_CONCURRENT), not a provider-pool refusal.
        raise V1ApiError(
            429,
            "concurrency_limit",
            "global concurrent-agent cap reached",
            retry_after=retry_after,
        )
    raise V1ApiError(
        429,
        "provider_exhausted",
        f"no account available for provider {provider!r}",
        retry_after=retry_after,
    )


def _release_lease(lease: Any) -> None:
    """Release one scheduler lease; idempotent and never raises."""
    if lease is not None:
        try:
            lease.release()
        except Exception:
            pass


def _release_agent_lease(v1: V1State, agent_id: str) -> None:
    """Pop and release the lease stored for ``agent_id`` (no-op when absent)."""
    _release_lease(v1.pop_lease(agent_id))


def _discard_agent(plane: Any, v1: V1State, agent_id: str, lease: Any = None) -> None:
    """Best-effort teardown of a half-created agent.

    Closes the session and frees the scheduler lease; secondary failures are
    swallowed so the original route error is never masked. Reaper / internal
    ``/api`` cleanup belongs to the control plane (P2-C), not this route.
    """
    try:
        plane.close(agent_id)
    except Exception:
        pass
    stored = v1.pop_lease(agent_id)
    _release_lease(stored)
    if lease is not None and lease is not stored:
        _release_lease(lease)


def _default_model(provider: str, account: Account | None) -> str | None:
    """Omitted ``AgentSpec.model`` → a valid provider/account default.

    The resolved account's first advertised model wins; otherwise the
    provider's seeded default. ``None`` defers to the plane's configured
    default (``gpt-5.6-luna``, codex backward compatibility).
    """
    if account is not None and account.models:
        return account.models[0]
    defaults = PROVIDER_DEFAULT_MODELS.get(provider) or ()
    return defaults[0] if defaults else None


def _workspace_error(exc: WorkspaceError) -> V1ApiError:
    """SOR-83 domain error → canonical v1 error (machine code preserved)."""
    if exc.code in (WORKSPACE_NOT_FOUND, ARTIFACT_NOT_FOUND):
        return V1ApiError(404, exc.code, exc.message)
    if exc.code == WORKSPACE_INVALID:
        return V1ApiError(400, exc.code, exc.message)
    return V1ApiError(409, exc.code, exc.message)


def _validate_workspace_decl(
    body: CreateAgentRequest, artifacts: Any
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Validate the SOR-83 ``workspace``/``handoff`` declarations.

    Handoff without a workspace is meaningless at create time (there is no
    prior record to apply onto), and a referenced artifact must exist in the
    durable store — both fail fast with explicit errors before any claim or
    sandbox work begins.
    """
    workspace = body.workspace.model_dump() if body.workspace is not None else None
    handoff = body.handoff.model_dump() if body.handoff is not None else None
    if workspace is not None:
        try:
            WorkspaceSpec(
                repo=workspace["repo"],
                base_ref=workspace["base_ref"],
                base_sha=workspace["base_sha"],
            )
        except WorkspaceError as exc:
            raise _workspace_error(exc) from exc
    if handoff is not None:
        handoff.pop("workspace", None)  # only meaningful on the handoff route
        has_artifact = bool(handoff.get("artifact_id"))
        has_head = bool(handoff.get("head_sha"))
        if has_artifact == has_head:
            raise V1ApiError(
                400,
                WORKSPACE_INVALID,
                "handoff needs exactly one of artifact_id or head_sha",
            )
        if workspace is None:
            raise V1ApiError(400, WORKSPACE_INVALID, "handoff requires a workspace declaration")
        if has_artifact:
            try:
                artifacts.manifest(handoff["artifact_id"])
            except ArtifactNotFoundError as exc:
                raise V1ApiError(
                    404, ARTIFACT_NOT_FOUND, f"unknown artifact {handoff['artifact_id']!r}"
                ) from exc
            except ArtifactError as exc:
                raise V1ApiError(409, "artifact_invalid", str(exc)) from exc
    return workspace, handoff


@router.post("/agents", status_code=201)
def create_agent(
    body: CreateAgentRequest,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    registry: AccountRegistry = Depends(get_registry),
    scheduler: Scheduler = Depends(get_scheduler),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    reporter: RunFailureReporter = Depends(get_run_reporter),
    artifacts: Any = Depends(get_artifact_store),
    workflows: WorkflowService = Depends(get_workflow_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Create an agent and queue its first run (SOR-82 A2).

    Returns as soon as the session record + a ``CREATING`` run-1 exist;
    sandbox cold start / ``runner init`` / first-turn dispatch run on a
    background worker and surface through ``GET`` polling. A retried
    ``Idempotency-Key`` with the same body replays the original response;
    a different body under a used key is a 409 ``idempotency_conflict``.

    SOR-83: ``workspace`` declares the checkout the run must start on;
    ``handoff`` makes run-1 start from a referenced artifact or commit.
    """
    workspace, handoff = _validate_workspace_decl(body, artifacts)
    owned = None
    fingerprint = request_fingerprint(body)
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
        # Owned the claim: now the durable bound. The session record pins
        # (api key, key), so a retry landing after a control-plane restart —
        # where the in-memory IdempotencyStore is empty — still resolves to
        # the original agent instead of provisioning a second worker.
        prior = plane.find_by_idempotency(key.id, idempotency_key)
        if prior is not None:
            if prior.idempotency_fingerprint not in (None, fingerprint):
                v1.idempotency.abandon(key.id, idempotency_key, entry)
                raise V1ApiError(
                    409,
                    "idempotency_conflict",
                    "Idempotency-Key was already used with a different request body",
                )
            if body.metadata is not None:
                # Idempotent upsert: covers the rare case where the first
                # attempt died between open_session and attach.
                workflows.attach(owner=key.id, agent_id=prior.id, metadata=body.metadata)
            pub = plane.public(prior)
            meta = _meta_for(v1, prior)
            result = {
                "agent": _agent_payload(plane, v1, workflows, prior),
                "run": _run_public(
                    plane,
                    pub,
                    prior,
                    1,
                    v1.cancelled(prior.id),
                    meta,
                    run_states,
                    scheduler=scheduler,
                    reporter=reporter,
                ),
            }
            v1.idempotency.complete(key.id, idempotency_key, entry, agent_id=prior.id, body=result)
            v1.idempotency.settle(key.id, idempotency_key, entry)
            return result
        owned = entry

    on_provisioned = None
    if owned is not None:
        # The claim resolves only once the worker's sandbox allocation has —
        # a duplicate that lands mid-provision waits instead of racing a
        # second backend.create.
        on_provisioned = lambda: v1.idempotency.settle(  # noqa: E731
            key.id, idempotency_key, owned
        )
    try:
        result = _create_agent_once(
            body,
            key,
            plane,
            registry,
            scheduler,
            v1,
            run_states,
            workflows,
            reporter=reporter,
            idempotency_key=idempotency_key,
            idempotency_fingerprint=fingerprint,
            on_provisioned=on_provisioned,
            workspace=workspace,
            handoff=handoff,
        )
    except Exception:
        if owned is not None:
            # Failed creates don't pin the key — a retry may proceed.
            v1.idempotency.abandon(key.id, idempotency_key, owned)
        raise
    if owned is not None:
        v1.idempotency.complete(
            key.id,
            idempotency_key,
            owned,
            agent_id=result["agent"]["id"],
            body=result,
        )
    return result


def _create_agent_once(
    body: CreateAgentRequest,
    key: ApiKey,
    plane: Any,
    registry: AccountRegistry,
    scheduler: Scheduler,
    v1: V1State,
    run_states: RunStateStore,
    workflows: WorkflowService,
    *,
    reporter: RunFailureReporter | None = None,
    idempotency_key: str | None = None,
    idempotency_fingerprint: str | None = None,
    on_provisioned: Any = None,
    workspace: dict[str, Any] | None = None,
    handoff: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider = body.agent.provider
    requested = body.agent.account_id or "auto"

    # P2.1's Devin pool exposes an atomic acquire() in addition to the frozen
    # consultative Scheduler.decide() port.  Use it when available so two
    # concurrent POSTs cannot both observe the same free slot.
    lease = None
    acquire = getattr(scheduler, "acquire", None)
    if callable(acquire):
        try:
            lease = acquire(provider=provider, account=requested)
        except ScheduleRefused as exc:
            _raise_schedule_error(
                exc.error,
                retry_after=exc.retry_after,
                provider=provider,
                requested=requested,
            )
            raise AssertionError("unreachable")
        account = lease.account
    else:
        decision = scheduler.decide(provider=provider, account=requested)
        _raise_schedule_error(
            decision.error,
            retry_after=decision.retry_after,
            provider=provider,
            requested=requested,
        )
        account = decision.account

    resolved = account.id if account is not None else requested
    secret_name = None
    if account is not None:
        secret_name = account.secret_name or None
    model = body.agent.model or _default_model(provider, account)

    try:
        session_id = plane.open_session(
            owner=key.id,
            title=body.name,
            model=model,
            provider=provider,
            account_id=resolved,
            first_prompt=body.prompt.text,
            idempotency_key=idempotency_key,
            idempotency_fingerprint=idempotency_fingerprint,
        )
    except ConcurrencyLimit as exc:
        _release_lease(lease)
        raise V1ApiError(
            429, "concurrency_limit", "per-key concurrent sandbox cap reached"
        ) from exc
    except Exception:
        _release_lease(lease)
        raise

    try:
        if lease is not None:
            v1.set_lease(session_id, lease)
        v1.set_meta(
            session_id,
            AgentMeta(
                provider=provider,
                account_id=resolved,
                name=body.name,
                idle_timeout_s=body.idle_timeout_s,
            ),
        )
        if account is not None:
            try:
                registry.touch(account.id, _iso_now())
            except KeyError:
                pass
        if body.metadata is not None:
            # SOR-84 C1: persist the caller's workflow/task binding before
            # the worker starts so recovery never sees an untracked agent.
            workflows.attach(owner=key.id, agent_id=session_id, metadata=body.metadata)
        # Backstop for ledger-less run-state seams: with the durable ledger
        # attached, open_session already persisted run-1 as CREATING and this
        # is an idempotent no-op.
        run_states.begin(session_id, 1, prompt=body.prompt.text)

        # The worker provisions the sandbox, runs ``runner init`` and
        # dispatches run-1; failures land as persisted run ERROR and release
        # the lease. If the thread itself cannot start, the discard below
        # frees the lease — otherwise the session would sit ``creating``
        # with a held lease until the reaper's create grace expires.
        launch_first_run(
            plane=plane,
            v1=v1,
            run_states=run_states,
            session_id=session_id,
            provider=provider,
            account_id=resolved,
            secret_name=secret_name,
            on_provisioned=on_provisioned,
            workspace=workspace,
            handoff=handoff,
        )
    except Exception:
        _discard_agent(plane, v1, session_id, lease)
        raise

    rec = _require_agent(plane, session_id)
    pub = plane.public(rec)
    agent = _agent_payload(plane, v1, workflows, rec)
    run = _run_public(
        plane,
        pub,
        rec,
        1,
        v1.cancelled(session_id),
        _meta_for(v1, rec),
        run_states,
        scheduler=scheduler,
        reporter=reporter,
    )
    return {"agent": agent, "run": run}


@router.get("/agents")
def list_agents(
    provider: ProviderId | None = None,
    account_id: str | None = None,
    status: str | None = None,
    workflow_id: str | None = None,
    cursor: str | None = None,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    workflows: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    start = 0
    if cursor:
        try:
            start = max(0, int(cursor))
        except ValueError:
            raise V1ApiError(400, "invalid_provider", "malformed cursor") from None
    records = plane.store.list_all()
    if workflow_id is not None:
        # SOR-84: index-backed scope — only agents whose durable binding
        # matches (caller key id, workflow_id) are listed.
        scoped = workflows.agent_ids(key.id, workflow_id)
        records = [rec for rec in records if rec.id in scoped]
    agents = [_agent_payload(plane, v1, workflows, rec) for rec in records]
    agents = [
        a
        for a in agents
        if (provider is None or a["provider"] == provider)
        and (account_id is None or a["account_id"] == account_id)
        and (status is None or a["status"] == status)
    ]
    agents.sort(key=lambda a: (a["created_at"], a["id"]))
    page = agents[start : start + AGENTS_PAGE_SIZE]
    next_cursor = str(start + AGENTS_PAGE_SIZE) if start + AGENTS_PAGE_SIZE < len(agents) else None
    return {"agents": page, "next_cursor": next_cursor}


@router.get("/agents/{agent_id}")
def get_agent(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    workflows: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    rec = _require_agent(plane, agent_id)
    return _agent_payload(plane, v1, workflows, rec)


@router.delete("/agents/{agent_id}")
def delete_agent(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    workflows: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    try:
        rec = plane.close(agent_id)
    except KeyError:
        raise not_found("agent not found") from None
    finally:
        # The account slot is freed even when close/terminate fails; deeper
        # sandbox cleanup stays with the control plane / reaper (P2-C).
        _release_agent_lease(v1, agent_id)
    return _agent_payload(plane, v1, workflows, rec)


# -------------------------------------------------------------- workflows


@router.get("/workflows/{workflow_id}")
def get_workflow(
    workflow_id: str,
    key: ApiKey = Depends(agents_key),
    workflows: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    """Workflow query / recover read (SOR-84).

    Agents + latest runs + progress for ``(caller key id, workflow_id)``,
    served from persisted records only — cheap enough to poll while a fresh
    client process re-attaches after losing local state.
    """
    view = workflows.lookup(key.id, workflow_id)
    if view is None:
        raise not_found("workflow not found")
    return view


@router.delete("/workflows/{workflow_id}")
def delete_workflow(
    workflow_id: str,
    key: ApiKey = Depends(agents_key),
    workflows: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    """Scoped cleanup: close exactly this workflow's agents (idempotent).

    Other workflows — and other principals' same-named workflows — are
    never touched; the per-agent owner is re-checked before close.
    """
    result = workflows.cleanup(key.id, workflow_id)
    if result is None:
        raise not_found("workflow not found")
    return result


# --------------------------------------------- workspaces + artifacts (SOR-83)


def _require_live_idle(plane: Any, agent_id: str) -> Any:
    """The session record for workspace-mutating routes: must exist, sit
    idle on a live sandbox (a running turn would mutate files mid-apply)."""
    rec = _require_agent(plane, agent_id)
    if rec.status == "running":
        raise V1ApiError(409, "turn_in_progress", "a run is in progress")
    if rec.status != "idle":
        raise V1ApiError(409, "session_not_runnable", f"agent status is {rec.status}")
    if rec.handle() is None:
        raise V1ApiError(409, "session_not_runnable", "agent has no live sandbox")
    return rec


@router.get("/agents/{agent_id}/workspace")
def get_workspace(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    workspaces: Any = Depends(get_workspaces),
) -> dict[str, Any]:
    """Durable workspace record: declared base, actual checkout/head, and
    the sha an independent reviewer pinned (``reviewed_head_sha``)."""
    _require_agent(plane, agent_id)
    record = workspaces.get(agent_id)
    if record is None:
        raise not_found("workspace not found")
    return {"workspace": workspace_record_to_dict(record)}


@router.post("/agents/{agent_id}/workspace/review")
def review_workspace(
    agent_id: str,
    body: ReviewWorkspaceRequest | None = None,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    workspaces: Any = Depends(get_workspaces),
) -> dict[str, Any]:
    """Pin ``reviewed_head_sha`` — the exact commit a reviewer signed off.

    ``head_sha`` defaults to the recorded head; an explicit value that
    disagrees with it is an explicit ``head_sha_mismatch``, never a silent
    mislabel.
    """
    _require_agent(plane, agent_id)
    try:
        record = workspaces.mark_reviewed(agent_id, body.head_sha if body else None)
    except WorkspaceError as exc:
        raise _workspace_error(exc) from exc
    return {"workspace": workspace_record_to_dict(record)}


@router.post("/agents/{agent_id}/handoff")
def apply_handoff(
    agent_id: str,
    body: HandoffRef,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    workspaces: Any = Depends(get_workspaces),
    handoffs: Any = Depends(get_handoffs),
) -> dict[str, Any]:
    """Apply a second-agent handoff into a live agent's workspace.

    ``artifact_id`` applies a durable artifact package; ``head_sha`` checks
    out an exact commit. Both validate the artifact's declared base against
    the workspace's recorded head before touching the workdir — a gap is an
    explicit ``base_sha_mismatch``.
    """
    rec = _require_live_idle(plane, agent_id)
    handle = rec.handle()
    has_artifact = bool(body.artifact_id)
    has_head = bool(body.head_sha)
    if has_artifact == has_head:
        raise V1ApiError(
            400, WORKSPACE_INVALID, "handoff needs exactly one of artifact_id or head_sha"
        )
    spec = None
    if body.workspace is not None:
        try:
            spec = WorkspaceSpec(
                repo=body.workspace.repo,
                base_ref=body.workspace.base_ref,
                base_sha=body.workspace.base_sha,
            )
        except WorkspaceError as exc:
            raise _workspace_error(exc) from exc
    try:
        if has_artifact:
            record = handoffs.prepare_from_artifact(handle, agent_id, body.artifact_id, spec=spec)
        else:
            record = handoffs.prepare_from_head(handle, agent_id, body.head_sha, spec=spec)
    except WorkspaceError as exc:
        raise _workspace_error(exc) from exc
    return {"workspace": workspace_record_to_dict(record)}


def _artifact_public(manifest: Any) -> dict[str, Any]:
    out = manifest_to_dict(manifest)
    out["download_url"] = f"/v1/artifacts/{manifest.artifact_id}/download"
    return out


def _artifact_forbidden(plane: Any, v1: V1State, registry: Any, rec: Any) -> tuple[bytes, ...]:
    """Secrets that must never enter this agent's artifact: the account's
    credential blob contents plus ambient credential env values."""
    account_id = _meta_for(v1, rec).account_id
    blob = None
    get_blob = getattr(registry, "get_credential_blob", None)
    if callable(get_blob) and account_id and account_id != "auto":
        try:
            blob = get_blob(account_id)
        except Exception:
            blob = None
    return credential_forbidden_values(blob)


@router.post("/agents/{agent_id}/artifacts", status_code=201)
def create_artifact(
    agent_id: str,
    body: CreateArtifactRequest | None = None,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    registry: AccountRegistry = Depends(get_registry),
    v1: V1State = Depends(get_v1_state),
    workspaces: Any = Depends(get_workspaces),
    artifacts: Any = Depends(get_artifact_store),
) -> dict[str, Any]:
    """Snapshot the agent's declared workspace into a durable artifact.

    Runs while the sandbox is still alive so the package outlives teardown.
    The manifest records base/head shas, per-file checksums, producer
    identity and any test result; ``run_id`` (default: the last run) gets an
    ``artifact://<id>`` ref persisted on its durable run record.
    """
    rec = _require_live_idle(plane, agent_id)
    run_id = body.run_id if body is not None else None
    run_n: int | None = None
    if run_id is not None:
        run_n = _run_n(run_id)
        if run_n is None:
            raise V1ApiError(400, "invalid_provider", f"malformed run_id {run_id!r}")
        if run_n not in _known_run_ns(rec, _ledger(plane)):
            raise not_found("run not found")
    elif rec.turns:
        run_n = int(rec.turns)
        run_id = f"run-{run_n}"
    try:
        manifest = snapshot_workspace_artifact(
            backend=plane.backend,
            handle=rec.handle(),
            workspaces=workspaces,
            store=artifacts,
            agent_id=agent_id,
            run_id=run_id,
            test_command=body.test_command if body is not None else None,
            forbidden_values=_artifact_forbidden(plane, v1, registry, rec),
            ledger=_ledger(plane),
            run_n=run_n,
        )
    except WorkspaceError as exc:
        raise _workspace_error(exc) from exc
    except ArtifactSecretError as exc:
        raise V1ApiError(409, "artifact_secret", str(exc)) from exc
    except ArtifactError as exc:
        raise V1ApiError(409, "artifact_invalid", str(exc)) from exc
    return {"artifact": _artifact_public(manifest)}


@router.get("/artifacts")
def list_artifacts(
    agent_id: str | None = None,
    key: ApiKey = Depends(agents_key),
    artifacts: Any = Depends(get_artifact_store),
) -> dict[str, Any]:
    """Durable artifact manifests (``?agent_id=`` filters by producer)."""
    return {"artifacts": [_artifact_public(m) for m in artifacts.list(agent_id=agent_id)]}


@router.get("/artifacts/{artifact_id}")
def get_artifact(
    artifact_id: str,
    key: ApiKey = Depends(agents_key),
    artifacts: Any = Depends(get_artifact_store),
) -> dict[str, Any]:
    """Artifact manifest: file checksums, base/head shas, producer identity."""
    try:
        manifest = artifacts.manifest(artifact_id)
    except ArtifactNotFoundError as exc:
        raise not_found("artifact not found") from exc
    except ArtifactCorruptError as exc:
        raise V1ApiError(409, "artifact_invalid", str(exc)) from exc
    return _artifact_public(manifest)


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(
    artifact_id: str,
    member: str = "patch.diff",
    key: ApiKey = Depends(agents_key),
    artifacts: Any = Depends(get_artifact_store),
) -> Response:
    """Download one member's bytes (``manifest.json``, ``patch.diff``,
    ``repo.bundle``, or ``files/<path>``). Checksum-verified on read; works
    after the producing sandbox is gone."""
    try:
        data = artifacts.read(artifact_id, member)
    except ArtifactNotFoundError as exc:
        raise not_found("artifact or member not found") from exc
    except ArtifactCorruptError as exc:
        raise V1ApiError(409, "artifact_invalid", str(exc)) from exc
    except ArtifactError as exc:
        raise V1ApiError(400, "invalid_provider", str(exc)) from exc
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"X-SBX-Artifact-Id": artifact_id},
    )


# ------------------------------------------------------------------- runs


@router.post("/agents/{agent_id}/runs", status_code=201)
def create_run(
    agent_id: str,
    body: CreateRunRequest,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: RunFailureReporter = Depends(get_run_reporter),
    workflows: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    _require_agent(plane, agent_id)
    try:
        turn_id = plane.post_message(agent_id, body.prompt.text)
    except KeyError:
        raise not_found("agent not found") from None
    except SessionConflict as exc:
        raise V1ApiError(exc.code, exc.error, exc.error) from exc
    if body.metadata is not None:
        # SOR-84: a follow-up may re-bind the agent's workflow task; the
        # run is already queued, so a refused message never re-binds.
        workflows.attach(owner=key.id, agent_id=agent_id, metadata=body.metadata)
    n = _turn_n(turn_id) or 0
    # Dispatched at once, so the run is born RUNNING (SOR-82 A2 seam).
    run_states.begin(agent_id, n, prompt=body.prompt.text, status="RUNNING")
    rec = _require_agent(plane, agent_id)
    pub = plane.public(rec)
    return _run_public(
        plane,
        pub,
        rec,
        n,
        v1.cancelled(agent_id),
        _meta_for(v1, rec),
        run_states,
        scheduler=scheduler,
        reporter=reporter,
    )


@router.get("/agents/{agent_id}/runs")
def list_runs(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: RunFailureReporter = Depends(get_run_reporter),
) -> dict[str, Any]:
    rec = _require_agent(plane, agent_id)
    return {"runs": _runs(plane, rec, v1, run_states, scheduler=scheduler, reporter=reporter)}


@router.get("/agents/{agent_id}/runs/{run_id}")
def get_run(
    agent_id: str,
    run_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: RunFailureReporter = Depends(get_run_reporter),
) -> dict[str, Any]:
    rec = _require_agent(plane, agent_id)
    return _require_run(plane, rec, run_id, v1, run_states, scheduler=scheduler, reporter=reporter)


@router.post("/agents/{agent_id}/runs/{run_id}/cancel")
def cancel_run(
    agent_id: str,
    run_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: RunFailureReporter = Depends(get_run_reporter),
) -> dict[str, Any]:
    rec = _require_agent(plane, agent_id)
    n = _run_n(run_id)
    if n is None or n not in _known_run_ns(rec, _ledger(plane), run_states):
        raise not_found("run not found")
    state = run_states.get(agent_id, n)
    if state is not None and state.status == "CREATING":
        # Pre-dispatch run-1 (SOR-82 A2): drop the queued turn so the worker
        # skips it, and persist CANCELLED — terminal, never resurrected.
        plane.discard_queued_first_turn(agent_id)
        run_states.transition(agent_id, n, "CANCELLED")
        v1.mark_cancelled(agent_id, n)
        rec = _require_agent(plane, agent_id)
        if rec.current_turn_n == n and rec.status == "running":
            # The worker dispatched between our read and the transition:
            # the turn just started — stop it so a cancelled run does not
            # keep executing billed work.
            try:
                plane.stop(agent_id)
            except Exception:
                pass
            rec = _require_agent(plane, agent_id)
    elif rec.current_turn_n == n and rec.status == "running":
        try:
            plane.stop(agent_id)
        except KeyError:
            raise not_found("agent not found") from None
        v1.mark_cancelled(agent_id, n)
        run_states.transition(agent_id, n, "CANCELLED")
        rec = _require_agent(plane, agent_id)
    return _require_run(plane, rec, run_id, v1, run_states, scheduler=scheduler, reporter=reporter)


# ------------------------------------------------------------------- SSE


def _belongs_to_run(obj: dict[str, Any], run_n: int, current_turn: int) -> tuple[bool, int]:
    """Track turn boundaries via ``sbx.turn_started``; report membership.

    Lines preceding the first ``sbx.turn_started`` (e.g. ``sbx.session_meta``)
    are attributed to run 1. ``id`` keeps the absolute events.jsonl line number
    so ``Last-Event-ID`` resume stays consistent with ``/api/*`` semantics.
    """
    if obj.get("type") == "sbx.turn_started":
        try:
            current_turn = int(obj.get("n") or 0)
        except (TypeError, ValueError):
            pass
    return (current_turn == run_n or (run_n == 1 and current_turn == 0)), current_turn


def _parse_event_line(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
        return {"type": "error", "message": raw}
    except json.JSONDecodeError:
        return {"type": "error", "message": "bad json in event stream"}


@router.get("/agents/{agent_id}/runs/{run_id}/stream")
async def stream_run(
    request: Request,
    agent_id: str,
    run_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
) -> Any:
    from control.app import DisconnectAwareStreamingResponse

    rec = _require_agent(plane, agent_id)
    n = _run_n(run_id)
    if n is None or n not in _known_run_ns(rec, _ledger(plane), run_states):
        raise not_found("run not found")
    try:
        last_id = int(last_event_id) if last_event_id else 0
    except ValueError:
        last_id = 0
    start_line = max(1, last_id + 1)

    backend = getattr(plane, "backend", None)
    keepalive_s: float = getattr(request.app.state, "keepalive_s", 15.0)

    def _live_handle() -> tuple[Any, Any]:
        """Current sandbox handle + poll, re-fetched so a CREATING run can
        attach once the background provisioner binds the sandbox (SOR-82 A2)."""
        if backend is None:
            return None, None
        rec_now = plane.get(agent_id)
        if rec_now is None:
            return None, None
        h = rec_now.handle()
        if h is None:
            return None, None
        try:
            p = backend.poll(h)
        except Exception:
            p = None
        return h, p

    async def gen() -> AsyncIterator[str]:
        proc: Any = None
        current_turn = 0
        try:
            yield ": keepalive\n\n"
            handle, poll = _live_handle()
            next_ka = time.monotonic() + keepalive_s
            # Wait out the CREATING window: the sandbox appears once the
            # background worker binds it; a terminal run/session exits to the
            # replay path below.
            while handle is None or poll is None or not poll.alive:
                state = run_states.get(agent_id, n)
                if state is not None and state.status in RUN_TERMINAL:
                    break
                rec_now = plane.get(agent_id)
                if rec_now is None or rec_now.status in ("closed", "timed_out", "lost"):
                    break
                now = time.monotonic()
                if now >= next_ka:
                    yield ": keepalive\n\n"
                    next_ka = now + keepalive_s
                await asyncio.sleep(0.05)
                handle, poll = _live_handle()
            if backend is not None and handle is not None and poll is not None and poll.alive:
                proc = await asyncio.to_thread(
                    backend.exec,
                    handle,
                    ["tail", "-n", "+1", "-F", str(handle.root / "events.jsonl")],
                    sandbox_env(handle),
                )
                line_q: queue.Queue[tuple[str, str | None]] = queue.Queue()

                def _reader() -> None:
                    try:
                        for line in proc.stdout:
                            line_q.put(("line", line))
                    except Exception:
                        pass
                    finally:
                        line_q.put(("eof", None))

                threading.Thread(target=_reader, daemon=True, name="sbx-v1-sse-tail").start()

                lineno = 0
                next_ka = time.monotonic() + keepalive_s
                while True:
                    try:
                        kind, payload = line_q.get_nowait()
                    except queue.Empty:
                        now = time.monotonic()
                        if now >= next_ka:
                            yield ": keepalive\n\n"
                            next_ka = now + keepalive_s
                        await asyncio.sleep(0.05)
                        continue
                    if kind == "eof":
                        break
                    raw = payload or ""
                    if not raw.strip():
                        continue
                    lineno += 1
                    obj = _parse_event_line(raw)
                    emit, current_turn = _belongs_to_run(obj, n, current_turn)
                    if emit and lineno >= start_line:
                        yield format_sse(lineno, obj)
                    now = time.monotonic()
                    if now >= next_ka:
                        yield ": keepalive\n\n"
                        next_ka = now + keepalive_s
                return

            # Sandbox unreachable: replay the run's slice if the file is
            # locally readable, then keep the stream open like /api/* does.
            lines: list[str] = []
            if backend is not None and handle is not None:
                try:
                    text = read_text(backend, handle, "events.jsonl")
                except Exception:
                    text = None
                if text:
                    lines = text.splitlines()
            lineno = 0
            for raw in lines:
                if not raw.strip():
                    continue
                lineno += 1
                obj = _parse_event_line(raw)
                emit, current_turn = _belongs_to_run(obj, n, current_turn)
                if emit and lineno >= start_line:
                    yield format_sse(lineno, obj)
            while True:
                await asyncio.sleep(keepalive_s)
                yield ": keepalive\n\n"
        finally:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass

    return DisconnectAwareStreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------------------------------ misc


@router.get("/agents/{agent_id}/usage")
def get_agent_usage(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
) -> dict[str, Any]:
    rec = _require_agent(plane, agent_id)
    pub = plane.public(rec)
    return {
        # None (never measured) serializes as null — unavailable, not
        # fabricated zeros (SOR-84).
        "usage": usage_public(rec.usage),
        "cost_estimate_usd": pub.get("cost_estimate_usd", 0.0),
        "sandbox_seconds": pub.get("sandbox_seconds", 0.0),
    }


@router.get("/models")
def list_models(
    key: ApiKey = Depends(agents_key),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Advertised models come from account declarations; availability counts
    active accounts with a free slot that list the model."""
    counts: dict[tuple[str, str], int] = {}
    for account in registry.list():
        free = (
            account.status == "active"
            and registry.running_count(account.id) < account.max_concurrent
        )
        for model in account.models:
            key_ = (account.provider, model)
            counts[key_] = counts.get(key_, 0) + (1 if free else 0)
    models = [
        {"provider": provider, "model": model, "accounts_available": count}
        for (provider, model), count in sorted(counts.items())
    ]
    return {"models": models}


@router.get("/me")
def get_me(key: ApiKey = Depends(api_key)) -> dict[str, Any]:
    return {"key_id": key.id, "label": key.label, "scopes": list(key.scopes)}


# --------------------------------------------------------------- accounts


@router.get("/accounts")
def list_accounts(
    provider: ProviderId | None = None,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return {
        "accounts": [
            account_public(account, registry.running_count(account.id))
            for account in registry.list(provider)
        ]
    }


@router.post("/accounts", status_code=201)
def create_account(
    body: CreateAccountRequest,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    account = Account(
        id=f"acct-{body.provider}-{uuid.uuid4().hex[:8]}",
        provider=body.provider,
        label=body.label,
        max_concurrent=body.max_concurrent,
        models=tuple(body.models),
        created_at=_iso_now(),
    )
    registry.put(account)
    if body.credential is not None:
        files = body.credential.get("files")
        if files is not None and (
            not isinstance(files, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in files.items())
        ):
            raise V1ApiError(400, "invalid_provider", "credential.files must be a string map")
        registry.put_credential_blob(
            account.id,
            {"provider": body.provider, "files": dict(files or {})},
        )
    return account_public(account, registry.running_count(account.id))


@router.get("/accounts/{account_id}")
def get_account(
    account_id: str,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    account = registry.get(account_id)
    if account is None:
        raise not_found("account not found")
    return account_public(account, registry.running_count(account.id))


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(
    account_id: str,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
) -> Response:
    if registry.get(account_id) is None:
        raise not_found("account not found")
    registry.remove(account_id)
    return Response(status_code=204)


@router.post("/accounts/{account_id}/verify")
def verify_account(
    account_id: str,
    key: ApiKey = Depends(admin_key),
    plane: Any = Depends(get_plane),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Probe the stored credential in a throwaway sandbox.

    Runs ``runner init --provider <account.provider>`` with the account's
    credential attached: the named Modal Secret when ``secret_name`` is set,
    else the local registry blob via ``SBX_ACCOUNT_CREDENTIAL`` /
    ``SBX_ACCOUNT_ID`` (restored under ``$SBX_WORK/home``). A non-zero init
    marks the account ``invalid``. When the plane exposes no usable backend the
    account is simply marked ``active`` (real per-provider CLI probes land with
    the P2-B adapters).
    """
    account = registry.get(account_id)
    if account is None:
        raise not_found("account not found")
    blob = registry.get_credential_blob(account_id)
    backend = getattr(plane, "backend", None)
    runner = getattr(plane, "runner", None)
    if backend is None or runner is None:
        return account_public(
            registry.mark_status(account_id, "active", last_error=None),
            registry.running_count(account_id),
        )
    handle = None
    try:
        from control.backend import SandboxSpec

        # Secret-only accounts carry their credential in the named Modal
        # Secret; a local registry blob travels via SBX_ACCOUNT_CREDENTIAL.
        secrets = [account.secret_name] if account.secret_name else []
        handle = backend.create(
            SandboxSpec(
                tags={
                    "purpose": _VERIFY_TAG,
                    "provider": account.provider,
                    "account_id": account_id,
                },
                secrets=secrets,
            )
        )
        verify_env: dict[str, str] = {"SBX_ACCOUNT_ID": account_id}
        if blob:
            verify_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(blob)
        env = sandbox_env(handle, verify_env)
        if not blob:
            # An empty or unrelated blob would shadow the named Secret.
            env.pop("SBX_ACCOUNT_CREDENTIAL", None)
        model = (
            _default_model(account.provider, account)
            or getattr(plane, "default_model", None)
            or "gpt-5.6-luna"
        )
        argv = runner(
            "init",
            "--auth",
            "auth_json",
            "--model",
            model,
            "--provider",
            account.provider,
            "--account-id",
            account_id,
        )
        proc = backend.exec(handle, argv, env=env)
        for _ in proc.stdout:
            pass
        code = proc.wait()
    except Exception:
        code = -1
    finally:
        if handle is not None:
            try:
                backend.terminate(handle)
            except Exception:
                pass
    if code == 0:
        updated = registry.mark_status(account_id, "active", last_error=None)
    elif code == 5:
        updated = registry.mark_status(account_id, "invalid", last_error="auth_invalid")
    elif code > 0:
        updated = registry.mark_status(account_id, "invalid", last_error="init_failed")
    else:
        updated = account
    return account_public(updated, registry.running_count(account_id))


# -------------------------------------------------------------- api keys


@router.get("/api-keys")
def list_api_keys(
    key: ApiKey = Depends(admin_key),
    store: ApiKeyStore = Depends(get_key_store),
) -> dict[str, Any]:
    return {"api_keys": [api_key_public(k) for k in store.list()]}


@router.post("/api-keys", status_code=201)
def create_api_key(
    body: CreateApiKeyRequest | None = None,
    key: ApiKey = Depends(admin_key),
    store: ApiKeyStore = Depends(get_key_store),
) -> dict[str, Any]:
    body = body or CreateApiKeyRequest()
    scopes = body.scopes if body.scopes is not None else ["agents"]
    if any(scope not in VALID_SCOPES for scope in scopes):
        raise V1ApiError(400, "invalid_provider", f"unknown scope; allowed: {list(VALID_SCOPES)}")
    record, token = store.create(label=body.label, scopes=scopes)
    return {**api_key_public(record), "key": token}


@router.delete("/api-keys/{key_id}", status_code=204)
def delete_api_key(
    key_id: str,
    key: ApiKey = Depends(admin_key),
    store: ApiKeyStore = Depends(get_key_store),
) -> Response:
    if not store.revoke(key_id):
        raise not_found("api key not found")
    return Response(status_code=204)
