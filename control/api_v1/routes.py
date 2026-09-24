"""Public ``/v1`` endpoints (Cursor Cloud Agents shape, ``api-v1.yaml``).

``agent ≙ session``, ``run ≙ turn``: ``POST /v1/agents`` creates a session and
immediately queues its first run; follow-ups are new runs on the same agent.
All endpoints consume ``ports.*`` Protocols plus the shared SessionService
(``app.state.plane``); provider / account metadata is tracked in ``V1State``
until P2-C persists it on the session record.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import queue
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Header, Request
from fastapi.responses import Response
from runtime.runner.contract import STATUS_SKIPPED, ContractError, normalize_contract
from runtime.runner.effort import effort_error, normalize_effort

from control.api_v1 import router
from control.api_v1.bootstrap import PROVIDER_DEFAULT_MODELS
from control.api_v1.deps import (
    RunFailureReporter,
    admin_key,
    agents_key,
    api_key,
    get_artifact_store,
    get_capabilities,
    get_github_app,
    get_handoffs,
    get_key_store,
    get_plane,
    get_registry,
    get_resources,
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
    GitHubAppAuthorizeCallbackRequest,
    HandoffRef,
    OutputContract,
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
    page_manifests,
)
from control.capabilities import CapabilitySnapshot, declared_snapshot
from control.compute import ComputeError, ComputeSpec, compute_for_record, resolve_compute
from control.config import TERMINAL_STATUSES, selected_providers
from control.credlifecycle import CredentialLifecycleService, CredentialRefresher
from control.credsync import TAG_CRED_RUN_FP, CredentialSync
from control.devin_pool import ScheduleRefused
from control.github_app import GitHubAppError
from control.latency import observe
from control.ports import Account, AccountRegistry, ApiKey, ApiKeyStore, Scheduler
from control.resources import ResourceError, resolve_resources, resource_refs
from control.run_errors import run_error_for_run
from control.run_store import (
    UNKNOWN_RUN_STATUS,
    RunRecord,
    apply_output_contract,
    contract_view,
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
    is_safe_ref,
    validate_git_policy,
)
from control.workspace import record_to_dict as workspace_record_to_dict

AGENTS_PAGE_SIZE = 100
ARTIFACTS_PAGE_MAX = 500  # SOR-201: cap per-page manifest fetches
_TURN_ID_RE = re.compile(r"^turn-(\d+)$")
_RUN_ID_RE = re.compile(r"^run-(\d+)$")
_VERIFY_TAG = "account-verify"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _registry_account(registry: AccountRegistry, account_id: str) -> Account:
    """``registry.get`` with 404 mapping. A non-conformant id is refused by
    the registry before any store access (SOR-105) — no such account can
    exist, so it surfaces as ``not_found`` rather than a 500."""
    try:
        account = registry.get(account_id)
    except ValueError:
        account = None
    if account is None:
        raise not_found("account not found")
    return account


def _running_or_zero(registry: AccountRegistry, account_id: str) -> int:
    """``running_count`` that treats a non-conformant stored id as 0 — such a
    record can never hold a session and must not 500 a listing (SOR-105)."""
    try:
        return registry.running_count(account_id)
    except ValueError:
        return 0


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
    resources: dict[str, Any] | None = None
    raw_resources = tags.get("resources")
    if raw_resources:
        try:
            parsed = json.loads(raw_resources)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            resources = parsed
    return AgentMeta(
        provider=tags.get("provider") or "codex",
        account_id=tags.get("account_id") or "auto",
        resources=resources,
        reasoning_effort=tags.get("reasoning_effort") or getattr(rec, "reasoning_effort", None),
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
        # SOR-179: the effective effort this run executed under.
        "reasoning_effort": record.reasoning_effort
        or (meta.reasoning_effort if meta else None)
        or pub.get("reasoning_effort"),
        "artifact_refs": list(record.artifact_refs),
        # SOR-130: extracted JSON + the contract verdict metadata (null when
        # the run carried no output contract).
        "structured_output": record.structured_output,
        "output_contract": contract_view(record),
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


def _with_skipped_contract(record: RunRecord) -> RunRecord:
    """Render-side copy of an open record whose run is reported terminal.

    The ledger record stays open (the verdict was never evaluated), so the
    contract must not read ``pending`` on a terminal run — report the same
    ``skipped`` a non-FINISHED persist would have written.
    """
    if record.output_contract is None or record.contract_result is not None:
        return record
    verdict = {
        "enforcement": record.output_contract.get("enforcement", "strict"),
        "schema_digest": record.output_contract.get("schema_digest"),
        "status": STATUS_SKIPPED,
        "extraction": None,
        "violations": [],
    }
    return replace(record, contract_result=verdict)


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
            credential_fp=(getattr(rec, "sandbox_tags", None) or {}).get(TAG_CRED_RUN_FP),
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
                _with_skipped_contract(record),
                pub,
                meta,
                status="CANCELLED",
                error=_run_error_public("CANCELLED", cancelled=True, agent_status=rec.status),
            )
        payload = _turn_payload(plane, rec, n)
        if payload is not None:
            status, error, result_text, usage = outcome_from_turn_payload(payload)
            # SOR-130: the read-path backfill must apply the same contract
            # enforcement as _finish_turn — a strict violation persists as
            # ERROR + contract_violation, never a silent FINISHED.
            status, error, structured_output, contract_result = apply_output_contract(
                status, error, result_text, record.output_contract
            )
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
                reasoning_effort=pub.get("reasoning_effort"),
                structured_output=structured_output,
                contract_result=contract_result,
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
                _with_skipped_contract(record),
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
        # SOR-130: no ledger record means no contract could be attached.
        "structured_output": None,
        "output_contract": None,
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
    if rec.status == "running":
        # A ``running`` record with no in-process watcher is stranded by a
        # control-plane cutover — the provider may already have written the
        # turn outcome. Settle it from evidence before answering so reads
        # report the truth and publish/handoff see an idle agent (SOR-139).
        reconcile = getattr(plane, "reconcile_turn", None)
        if callable(reconcile) and reconcile(agent_id):
            rec = plane.get(agent_id) or rec
    return rec


# Sentinel: ``task`` not supplied → resolve the binding serially (single
# record reads); ``None`` → pre-resolved as "no binding" by a batch pass.
_TASK_UNRESOLVED = object()


def _agent_payload(
    plane: Any,
    v1: V1State,
    workflows: WorkflowService,
    rec: Any,
    *,
    task: Any = _TASK_UNRESOLVED,
) -> dict[str, Any]:
    """Agent view: contract fields + workflow binding + honest usage.

    ``usage`` comes from the session record — ``None`` (never measured)
    serializes as ``null``, never fabricated zeros (SOR-84). ``metadata``
    echoes the durable workflow/task binding when one is attached. List
    paths pass a batch-resolved ``task`` so the per-agent Dict read is
    amortized into one store pass (SOR-200).
    """
    if task is _TASK_UNRESOLVED:
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
    # SOR-181: the echo resolves through the same durable-state lookup
    # as provisioning/cost so tag-only records report their sizing too.
    _compute = compute_for_record(getattr(rec, "compute", None), getattr(rec, "sandbox_tags", None))
    return agent_public(
        plane.public(rec),
        _meta_for(v1, rec),
        usage=rec.usage,
        metadata=metadata,
        compute=_compute.public() if _compute is not None else None,
    )


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
            "global live-agent cap (SBX_MAX_CONCURRENT) reached — "
            "idle agents hold slots until closed",
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


def _snapshot_for(
    capabilities: Any, account: Account | None, *, ensure: bool = True
) -> CapabilitySnapshot | None:
    """Catalog snapshot for the resolved account.

    ``None`` only when there is no account. A missing or non-catalog
    ``capabilities`` (e.g. a direct unit call) degrades to declared rows —
    the endpoint always advertises the models a seeded account carries.
    ``ensure=False`` serves the current cache only — used on the agent
    create path so a request can never spawn a probe sandbox as a
    side-effect of validation.
    """
    if account is None:
        return None
    if capabilities is None or not callable(getattr(capabilities, "get", None)):
        return declared_snapshot(account)
    try:
        return capabilities.get(account, ensure=ensure)
    except TypeError:
        return capabilities.get(account)
    except Exception:
        return declared_snapshot(account)


def _model_row(snapshot: CapabilitySnapshot, model: str | None) -> Any:
    """The capability row for ``model`` (id or alias); ``None`` if absent."""
    if model is None:
        return None
    for m in snapshot.models:
        if m.model == model or model in m.aliases:
            return m
    return None


def _default_model(provider: str, account: Account | None, capabilities: Any = None) -> str | None:
    """Omitted ``AgentSpec.model`` → a valid provider/account default.

    SOR-204: the capability catalog's default wins — a discovered snapshot
    reflects what the account actually serves. Then the account's declared
    models, then the provider's seeded default. ``None`` defers to the
    plane's configured default (``gpt-5.6-luna``, codex compatibility).
    """
    snapshot = _snapshot_for(capabilities, account, ensure=False)
    if snapshot is not None and snapshot.default_model:
        return snapshot.default_model
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
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Validate the SOR-83 ``workspace``/``handoff`` and SOR-128 ``git``
    declarations.

    Handoff without a workspace is meaningless at create time (there is no
    prior record to apply onto), a referenced artifact must exist in the
    durable store, and a git policy only makes sense on a declared repo —
    all fail fast with explicit errors before any claim or sandbox work
    begins.
    """
    workspace = body.workspace.model_dump() if body.workspace is not None else None
    handoff = body.handoff.model_dump() if body.handoff is not None else None
    git = body.git.model_dump(exclude_none=True) if body.git is not None else None
    if workspace is not None:
        try:
            WorkspaceSpec(
                repo=workspace["repo"],
                base_ref=workspace["base_ref"],
                base_sha=workspace["base_sha"],
            )
        except WorkspaceError as exc:
            raise _workspace_error(exc) from exc
    if git is not None:
        if workspace is None:
            raise V1ApiError(400, WORKSPACE_INVALID, "git policy requires a workspace declaration")
        try:
            validate_git_policy(git)
        except WorkspaceError as exc:
            raise _workspace_error(exc) from exc
    if handoff is not None:
        handoff.pop("workspace", None)  # only meaningful on the handoff route
        has_artifact = bool(handoff.get("artifact_id"))
        has_head = bool(handoff.get("head_sha"))
        has_pr = bool(handoff.get("pull_request"))
        if sum((has_artifact, has_head, has_pr)) != 1:
            raise V1ApiError(
                400,
                WORKSPACE_INVALID,
                "handoff needs exactly one of artifact_id, head_sha or pull_request",
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
        if has_pr:
            pr = handoff["pull_request"]
            if not is_safe_ref(pr.get("ref")):
                raise V1ApiError(
                    400, WORKSPACE_INVALID, f"unsafe pull_request ref: {pr.get('ref')!r}"
                )
    return workspace, handoff, git


def _normalize_contract(contract: OutputContract | None) -> dict[str, Any] | None:
    """Validate + normalize an ``output_contract`` declaration (SOR-130).

    A malformed body — bad enforcement, non-object schema, or a schema using
    keywords outside the deterministic validator subset — is refused as
    ``400 invalid_output_contract`` before any run is allocated, so an
    unenforceable contract can never reach a sandbox.
    """
    if contract is None:
        return None
    try:
        return normalize_contract(contract.model_dump(by_alias=True))
    except ContractError as exc:
        raise V1ApiError(400, "invalid_output_contract", str(exc)) from exc
    except Exception as exc:
        # normalize_contract only raises ContractError by design; anything
        # else (e.g. a schema too deep to serialize) is still a refused
        # contract — 400, never an uncaught 500.
        raise V1ApiError(
            400, "invalid_output_contract", f"unusable output contract: {type(exc).__name__}"
        ) from exc


def _validate_compute(body: CreateAgentRequest) -> ComputeSpec:
    """Validate + resolve the SOR-181 ``compute`` declaration.

    Always returns a concrete spec — an omitted declaration resolves to
    the canonical defaults so the durable record carries the sandbox's
    real sizing. Malformed/inverted/out-of-bounds values fail as
    ``400 invalid_compute`` before any claim or sandbox work begins.
    """
    try:
        return resolve_compute(body.compute)
    except ComputeError as exc:
        raise V1ApiError(400, exc.code, exc.message) from exc


def _validate_reasoning_effort(body: CreateAgentRequest) -> str | None:
    """Validate a declared canonical ``reasoning_effort`` (SOR-179/204).

    Only the canonical shape is checked here — whether the *account* can
    honor the level depends on discovered capabilities, which are known
    only after the scheduler resolves the account; that check lives in
    ``_create_agent_once`` (``_check_effort_capability``).
    """
    try:
        return normalize_effort(body.agent.reasoning_effort)
    except ValueError as exc:
        raise V1ApiError(400, "unsupported", str(exc)) from exc


def _check_effort_capability(
    provider: str, model: str | None, effort: str, snapshot: CapabilitySnapshot | None
) -> None:
    """Refuse an effort the resolved account/model cannot honor (SOR-204).

    A discovered snapshot is authoritative — the row's ``reasoning_efforts``
    decide. Declared/env/static rows carry the provider's verified floor
    only where effort is an orthogonal CLI flag (codex, grok); an empty
    list there means the provider has no native surface at all. No
    snapshot → the static floor applies.
    """
    refusal: str | None = None
    if snapshot is not None:
        row = _model_row(snapshot, model)
        if row is not None:
            if not row.reasoning_efforts:
                refusal = f"model {model!r} on {provider!r} has no native effort surface"
            elif effort not in row.reasoning_efforts:
                refusal = (
                    f"model {model!r} does not support reasoning_effort {effort!r} "
                    f"(supported: {list(row.reasoning_efforts)})"
                )
        elif snapshot.source == "discovered":
            refusal = f"model {model!r} is not advertised by this account"
        else:
            refusal = effort_error(provider, effort)
    else:
        refusal = effort_error(provider, effort)
    if refusal is not None:
        raise V1ApiError(400, "unsupported", refusal)


def _validate_resources(body: CreateAgentRequest, registry: Any) -> dict[str, Any] | None:
    """Validate + resolve SOR-129 ``resources`` refs against the registry.

    Unknown/disallowed refs fail as ``400 invalid_resource``; MCP refs on a
    provider with no MCP channel fail as ``400 unsupported`` — both before
    any claim or sandbox work begins, never silently ignored.
    """
    try:
        return resolve_resources(body.resources, provider=body.agent.provider, registry=registry)
    except ResourceError as exc:
        raise V1ApiError(400, exc.code, exc.message) from exc


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
    resources_registry: Any = Depends(get_resources),
    capabilities: Any = Depends(get_capabilities),
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
    workspace, handoff, git = _validate_workspace_decl(body, artifacts)
    contract = _normalize_contract(body.output_contract)
    compute = _validate_compute(body)
    effort = _validate_reasoning_effort(body)
    resources = _validate_resources(body, resources_registry)
    if contract is not None and _ledger(plane) is None:
        # Contracted runs need the durable ledger for both dispatch and the
        # persisted verdict — refuse rather than run uncontracted.
        raise V1ApiError(409, "session_not_runnable", "output contracts require the run ledger")
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
            capabilities=capabilities,
            reporter=reporter,
            idempotency_key=idempotency_key,
            idempotency_fingerprint=fingerprint,
            on_provisioned=on_provisioned,
            workspace=workspace,
            handoff=handoff,
            git=git,
            output_contract=contract,
            resources=resources,
            compute=compute,
            reasoning_effort=effort,
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
    capabilities: Any = None,
    reporter: RunFailureReporter | None = None,
    idempotency_key: str | None = None,
    idempotency_fingerprint: str | None = None,
    on_provisioned: Any = None,
    workspace: dict[str, Any] | None = None,
    handoff: dict[str, Any] | None = None,
    git: dict[str, Any] | None = None,
    output_contract: dict[str, Any] | None = None,
    resources: dict[str, Any] | None = None,
    compute: ComputeSpec | None = None,
    reasoning_effort: str | None = None,
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
    snapshot = _snapshot_for(capabilities, account, ensure=False)
    model = body.agent.model or _default_model(provider, account, capabilities)

    # SOR-204: reject combinations the account cannot serve. A discovered
    # snapshot is authoritative — an explicit model it does not advertise
    # fails fast instead of surfacing as a provider error mid-run.
    if (
        body.agent.model
        and snapshot is not None
        and snapshot.source == "discovered"
        and _model_row(snapshot, body.agent.model) is None
    ):
        _release_lease(lease)
        advertised = [m.model for m in snapshot.models]
        raise V1ApiError(
            400,
            "unsupported",
            f"model {body.agent.model!r} is not advertised by account "
            f"{account.id!r} (available: {advertised})",
        )
    if reasoning_effort is not None:
        try:
            _check_effort_capability(provider, model, reasoning_effort, snapshot)
        except V1ApiError:
            _release_lease(lease)
            raise

    # SOR-204: carry the resolved row's effort surface to init so the
    # in-sandbox backstop validates the declaration against the same
    # (possibly discovered-widened) surface the API just checked.
    surface_row = _model_row(snapshot, model) if snapshot is not None else None
    effort_surface = list(surface_row.reasoning_efforts) if surface_row is not None else None

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
            output_contract=output_contract,
            resource_refs=resource_refs(resources),
            compute=compute,
            reasoning_effort=reasoning_effort,
            effort_surface=effort_surface,
        )
    except ConcurrencyLimit as exc:
        _release_lease(lease)
        raise V1ApiError(
            429,
            "concurrency_limit",
            "per-key live-agent cap (SBX_MAX_CONCURRENT) reached — "
            "idle agents hold slots until closed",
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
                resources=resource_refs(resources),
                reasoning_effort=reasoning_effort,
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
            git=git,
            resources=resources,
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
    # SOR-200: the independent store passes run concurrently — the
    # session listing, the workflow scope index (when filtered) and the
    # binding scan each cost a remote round-trip, so serializing them
    # would multiply the page latency; per-agent reads would be an N+1.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        records_fut = pool.submit(plane.store.list_all)
        scoped_fut = (
            pool.submit(workflows.agent_ids, key.id, workflow_id)
            if workflow_id is not None
            else None
        )
        bindings_fut = pool.submit(workflows.all_bindings)
        records = records_fut.result()
        if scoped_fut is not None:
            # SOR-84: index-backed scope — only agents whose durable
            # binding matches (caller key id, workflow_id) are listed.
            scoped = scoped_fut.result()
            records = [rec for rec in records if rec.id in scoped]
        try:
            bindings = bindings_fut.result()
        except Exception:
            # Same degradation as a failed serial ``for_agent``: echo
            # no metadata rather than fail the whole listing.
            bindings = {}
    # Filter, sort and page on record-level fields *before* enrichment —
    # provider/account/status derive from the durable record and
    # in-process meta/tags, so off-page and filtered-out rows never pay
    # a payload build.
    rows: list[tuple[Any, AgentMeta]] = []
    for rec in records:
        meta = _meta_for(v1, rec)
        if provider is not None and (meta.provider or "codex") != provider:
            continue
        if account_id is not None and (meta.account_id or "auto") != account_id:
            continue
        public_status = "idle" if rec.status == "suspended" else rec.status
        if status is not None and public_status != status:
            continue
        rows.append((rec, meta))
    rows.sort(key=lambda row: (row[0].created_at.isoformat(), row[0].id))
    page_rows = rows[start : start + AGENTS_PAGE_SIZE]
    next_cursor = str(start + AGENTS_PAGE_SIZE) if start + AGENTS_PAGE_SIZE < len(rows) else None
    agents = [
        _agent_payload(plane, v1, workflows, rec, task=bindings.get(rec.id))
        for rec, _meta in page_rows
    ]
    return {"agents": agents, "next_cursor": next_cursor}


@router.get("/agents/summary")
def agents_summary(
    provider: ProviderId | None = None,
    account_id: str | None = None,
    status: str | None = None,
    workflow_id: str | None = None,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    workflows: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    """Cheap rollup the Console polls instead of a full ``GET /v1/agents``.

    Applies the same record-level filters as the list route but skips the
    per-agent payload build and the all-bindings scan: one store pass.
    ``version`` pins ``{total}:{max updated_at}`` so clients only refetch
    the expensive page when the set actually changed (SOR-202).
    """
    records = plane.store.list_all()
    if workflow_id is not None:
        try:
            scoped = workflows.agent_ids(key.id, workflow_id)
        except Exception:
            scoped = set()
        records = [rec for rec in records if rec.id in scoped]
    by_status: dict[str, int] = {}
    latest: datetime | None = None
    live = 0
    for rec in records:
        meta = _meta_for(v1, rec)
        if provider is not None and (meta.provider or "codex") != provider:
            continue
        if account_id is not None and (meta.account_id or "auto") != account_id:
            continue
        public_status = "idle" if rec.status == "suspended" else rec.status
        if status is not None and public_status != status:
            continue
        by_status[public_status] = by_status.get(public_status, 0) + 1
        if public_status in ("creating", "idle", "running"):
            live += 1
        if latest is None or rec.updated_at > latest:
            latest = rec.updated_at
    total = sum(by_status.values())
    return {
        "total": total,
        "live": live,
        "by_status": by_status,
        "version": f"{total}:{latest.isoformat() if latest else ''}",
    }


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
    try:
        with observe("v1.workspace.get", agent_id=agent_id):
            record = workspaces.get(agent_id)
    except WorkspaceError as exc:
        raise _workspace_error(exc) from exc
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

    ``comment`` (SOR-128) additionally posts a machine-readable comment on
    the workspace's recorded pull request — never a formal review approval
    under the shared GitHub identity. Commenting requires the agent's live
    sandbox.
    """
    comment = body.comment if body else None
    rec = _require_agent(plane, agent_id)
    try:
        with observe("v1.workspace.review", agent_id=agent_id):
            record = workspaces.mark_reviewed(agent_id, body.head_sha if body else None)
            if comment:
                if rec.status == "running":
                    raise V1ApiError(409, "turn_in_progress", "a run is in progress")
                handle = rec.handle()
                if handle is None:
                    raise V1ApiError(409, "session_not_runnable", "comment needs a live sandbox")
                record = workspaces.post_review_comment(handle, agent_id, comment)
    except WorkspaceError as exc:
        raise _workspace_error(exc) from exc
    return {"workspace": workspace_record_to_dict(record)}


@router.post("/agents/{agent_id}/git/publish")
def publish_git(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    workspaces: Any = Depends(get_workspaces),
) -> dict[str, Any]:
    """Execute the agent's declared git policy (SOR-128).

    Refreshes the recorded head, pushes the work branch to the workspace
    repo's remote, verifies the remote head (drift fails closed), and —
    when the policy's ``auto_create_pr`` is set — opens the declared pull
    request. ``pushed_head_sha`` / ``pull_request`` land on the durable
    workspace record so a reviewer can pin the exact published head.
    """
    rec = _require_live_idle(plane, agent_id)
    try:
        with observe("v1.git.publish", agent_id=agent_id):
            record = workspaces.publish(rec.handle(), agent_id)
    except WorkspaceError as exc:
        raise _workspace_error(exc) from exc
    return {"workspace": workspace_record_to_dict(record)}


@router.post("/agents/{agent_id}/git/merge")
def merge_git(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    workspaces: Any = Depends(get_workspaces),
) -> dict[str, Any]:
    """Merge the recorded pull request — review-gated (SOR-178).

    Requires the policy's ``merge`` flag, a recorded pull request, and an
    independent exact-sha review pin (``reviewed_head_sha`` set via
    ``POST /agents/{id}/workspace/review``). The recorded PR head and the
    remote PR ref must still equal the pin — any drift is
    ``head_sha_mismatch`` and needs a fresh review; an absent pin is
    ``review_required``. Merge metadata lands on ``workspace.merge``.
    """
    rec = _require_live_idle(plane, agent_id)
    try:
        with observe("v1.git.merge", agent_id=agent_id):
            record = workspaces.merge(rec.handle(), agent_id)
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
    out an exact commit; ``pull_request`` fetches a remote ref pinned to an
    exact head (SOR-128 — drift fails closed). All validate against the
    workspace's recorded head before touching the workdir — a gap is an
    explicit ``base_sha_mismatch`` / ``head_sha_mismatch``.
    """
    rec = _require_live_idle(plane, agent_id)
    handle = rec.handle()
    has_artifact = bool(body.artifact_id)
    has_head = bool(body.head_sha)
    has_pr = body.pull_request is not None
    if sum((has_artifact, has_head, has_pr)) != 1:
        raise V1ApiError(
            400,
            WORKSPACE_INVALID,
            "handoff needs exactly one of artifact_id, head_sha or pull_request",
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
        with observe("v1.workspace.handoff", agent_id=agent_id):
            if has_artifact:
                record = handoffs.prepare_from_artifact(
                    handle, agent_id, body.artifact_id, spec=spec
                )
            elif has_pr:
                record = handoffs.prepare_from_pull_request(
                    handle,
                    agent_id,
                    body.pull_request.ref,
                    body.pull_request.head_sha,
                    spec=spec,
                )
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
        with observe("v1.artifact.create", agent_id=agent_id, run_id=run_id):
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
    cursor: str | None = None,
    limit: int | None = None,
    key: ApiKey = Depends(agents_key),
    artifacts: Any = Depends(get_artifact_store),
) -> dict[str, Any]:
    """Durable artifact manifests, ``(created_at, artifact_id)`` keyset order.

    ``?agent_id=`` filters by producer through the durable per-agent
    index — the query reads one index document plus the page's manifests,
    never scans the whole store (SOR-201). ``?limit=`` pages the result
    and ``next_cursor`` resumes it; omit both for the full listing.
    """
    if limit is not None and not 1 <= limit <= ARTIFACTS_PAGE_MAX:
        raise V1ApiError(400, "invalid_provider", f"limit must be 1..{ARTIFACTS_PAGE_MAX}")
    list_page = getattr(artifacts, "list_page", None)
    try:
        with observe("v1.artifact.list", agent_id=agent_id):
            if callable(list_page):
                page = list_page(agent_id=agent_id, cursor=cursor, limit=limit)
            else:
                # Stores without list_page (custom injects): page in memory
                # over the full listing so the route contract still holds.
                page = page_manifests(artifacts.list(agent_id=agent_id), cursor=cursor, limit=limit)
    except ArtifactError as exc:
        raise V1ApiError(400, "invalid_provider", str(exc)) from exc
    return {
        "artifacts": [_artifact_public(m) for m in page.artifacts],
        "next_cursor": page.next_cursor,
    }


@router.get("/artifacts/{artifact_id}")
def get_artifact(
    artifact_id: str,
    key: ApiKey = Depends(agents_key),
    artifacts: Any = Depends(get_artifact_store),
) -> dict[str, Any]:
    """Artifact manifest: file checksums, base/head shas, producer identity."""
    try:
        with observe("v1.artifact.get", artifact_id=artifact_id):
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
        with observe("v1.artifact.download", artifact_id=artifact_id, member=member):
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
    contract = _normalize_contract(body.output_contract)
    if contract is not None and _ledger(plane) is None:
        # Contracted runs need the durable ledger for both dispatch and the
        # persisted verdict — refuse rather than run uncontracted.
        raise V1ApiError(409, "session_not_runnable", "output contracts require the run ledger")
    try:
        turn_id = plane.post_message(agent_id, body.prompt.text, output_contract=contract)
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
                    # Contract: id is the events.jsonl 1-based line number —
                    # a blank/torn line still consumes one (``/api/*`` parity).
                    lineno += 1
                    if not raw.strip():
                        continue
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
                lineno += 1
                if not raw.strip():
                    continue
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


def _capability_rows(registry: AccountRegistry, capabilities: Any) -> list[dict[str, Any]]:
    """One row per (account, model) from the capability catalog.

    SOR-204: each row carries the full capability surface — display name,
    family, aliases, canonical efforts with the provider-native map,
    availability, discovery provenance (``source``/``refreshed_at``/
    ``stale``) — plus ``accounts_available`` for compatibility.
    """
    enabled = frozenset(selected_providers())
    rows: list[dict[str, Any]] = []
    for account in registry.list():
        # Durable registries can retain accounts from an earlier deployment
        # with a wider provider set. Never advertise a provider whose image
        # and credential mounts are intentionally absent from this deploy.
        if account.provider not in enabled:
            continue
        snapshot = _snapshot_for(capabilities, account)
        if snapshot is None:
            continue
        active = account.status == "active"
        free = active and _running_or_zero(registry, account.id) < account.max_concurrent
        availability = "available" if free else ("busy" if active else "unavailable")
        for m in snapshot.models:
            rows.append(
                {
                    "provider": account.provider,
                    "account": account.id,
                    "model": m.model,
                    "display_name": m.display_name,
                    "family": m.family,
                    "aliases": list(m.aliases),
                    "reasoning_efforts": list(m.reasoning_efforts),
                    "effort_native": dict(m.effort_native),
                    "default_effort": m.default_effort,
                    "availability": availability,
                    "accounts_available": 0,
                    "source": snapshot.source,
                    "refreshed_at": snapshot.refreshed_at,
                    "stale": snapshot.stale,
                }
            )
    free_counts: dict[tuple[str, str], int] = {}
    for row in rows:
        if row["availability"] == "available":
            key_ = (row["provider"], row["model"])
            free_counts[key_] = free_counts.get(key_, 0) + 1
    for row in rows:
        row["accounts_available"] = free_counts.get((row["provider"], row["model"]), 0)
    rows.sort(key=lambda r: (r["provider"], r["model"], r["account"]))
    return rows


@router.get("/models")
def list_models(
    key: ApiKey = Depends(agents_key),
    registry: AccountRegistry = Depends(get_registry),
    capabilities: Any = Depends(get_capabilities),
) -> dict[str, Any]:
    """Advertised models come from the capability catalog (SOR-204): live
    CLI discovery per account with TTL + stale-last-good, falling back to
    declared/env/static models until the first probe lands."""
    return {"models": _capability_rows(registry, capabilities)}


@router.post("/models/refresh")
def refresh_models(
    provider: str | None = None,
    account_id: str | None = None,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
    capabilities: Any = Depends(get_capabilities),
) -> dict[str, Any]:
    """Synchronous capability refresh (SOR-204).

    Re-probes the matching accounts' provider CLIs and returns the updated
    catalog rows. Failed probes keep serving last-good data marked
    ``stale`` — refresh never empties the catalog.
    """
    if provider is not None and provider not in PROVIDER_DEFAULT_MODELS:
        raise V1ApiError(400, "invalid_provider", f"unknown provider {provider!r}")
    refreshed = 0
    for account in registry.list(provider):
        if account_id is not None and account.id != account_id:
            continue
        capabilities.refresh(account, registry.get_credential_blob(account.id))
        refreshed += 1
    return {"refreshed": refreshed, "models": _capability_rows(registry, capabilities)}


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
            account_public(account, _running_or_zero(registry, account.id))
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
    files: Any = None
    if body.credential is not None:
        files = body.credential.get("files")
        if files is not None and (
            not isinstance(files, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in files.items())
        ):
            # Validate before any write: a refused create must not leave an
            # active, credential-less account the scheduler can pick.
            raise V1ApiError(400, "invalid_provider", "credential.files must be a string map")
    registry.put(account)
    if body.credential is not None:
        blob = {"provider": body.provider, "files": dict(files or {})}
        registry.put_credential_blob(account.id, blob)
        try:
            CredentialLifecycleService(registry).note_credential(account.id, blob)
        except Exception:
            pass
    return account_public(account, _running_or_zero(registry, account.id))


@router.get("/accounts/{account_id}")
def get_account(
    account_id: str,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    account = _registry_account(registry, account_id)
    return account_public(account, _running_or_zero(registry, account.id))


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(
    account_id: str,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
) -> Response:
    _registry_account(registry, account_id)
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
    account = _registry_account(registry, account_id)
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
    try:
        lifecycle = CredentialLifecycleService(registry)
        if code == 0 and blob:
            lifecycle.note_credential(account_id, blob)
        elif code == 5:
            lifecycle.on_auth_invalid(account_id, detail="auth_invalid", mark_account=False)
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


@router.get("/accounts/{account_id}/lifecycle")
def account_credential_lifecycle(
    account_id: str,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Non-secret credential lifecycle metadata (SOR-176).

    States: ``healthy`` / ``access_expiring`` / ``refreshing`` /
    ``healthy_refreshed`` / ``reauth_required`` / ``revoked``.
    """
    account = _registry_account(registry, account_id)
    return {
        "account_id": account.id,
        "credential_lifecycle": CredentialLifecycleService(registry).describe(account_id),
    }


@router.post("/accounts/{account_id}/lifecycle/refresh")
def refresh_account_credential(
    account_id: str,
    key: ApiKey = Depends(admin_key),
    plane: Any = Depends(get_plane),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Run one synchronous credential refresh through the worker path (SOR-176).

    Reuses the app's background refresher when the plane has one; otherwise
    builds an ad-hoc refresher over the plane's backend. Returns the refresh
    outcome plus the resulting lifecycle metadata — never token material.
    """
    _registry_account(registry, account_id)
    refresher = getattr(plane, "credential_refresher", None)
    backend = getattr(plane, "backend", None)
    if refresher is None:
        if backend is None:
            raise V1ApiError(503, "unavailable", "no backend for credential refresh")
        sync = getattr(plane, "credential_sync", None) or CredentialSync(lambda: registry)
        refresher = CredentialRefresher(
            registry_source=lambda: registry,
            backend=backend,
            runner_cmd=list(getattr(plane, "runner_cmd", []) or []),
            sync=sync,
            lifecycle=CredentialLifecycleService(registry),
            default_model=getattr(plane, "default_model", None) or "gpt-5.6-luna",
        )
    try:
        return refresher.refresh_account(account_id)
    except Exception:
        raise V1ApiError(500, "internal", "credential refresh failed") from None


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


# ---------------------------------------------------------------------------
# GitHub App authorization (SOR-177)
# ---------------------------------------------------------------------------


@router.get("/github/app")
def github_app_status(
    key: ApiKey = Depends(agents_key),
    app: Any = Depends(get_github_app),
) -> dict[str, Any]:
    """Authorization posture: app configured?, installations, bridge fallback."""
    try:
        return app.status()
    except GitHubAppError as exc:
        raise _github_app_error(exc) from exc


@router.post("/github/app/authorize", status_code=201)
def github_app_begin_authorize(
    key: ApiKey = Depends(agents_key),
    app: Any = Depends(get_github_app),
) -> dict[str, Any]:
    """One-click connect, step 1: return the GitHub install URL to open."""
    try:
        return app.begin_authorization()
    except GitHubAppError as exc:
        raise _github_app_error(exc) from exc


@router.post("/github/app/authorize/callback")
def github_app_authorize_callback(
    body: GitHubAppAuthorizeCallbackRequest,
    key: ApiKey = Depends(agents_key),
    app: Any = Depends(get_github_app),
) -> dict[str, Any]:
    """Step 2: record an installation selected in the browser.

    ``{"installation_id": <int>, "state": "<from authorize>"}`` — the
    single-use ``state`` from step 1 is the callback's credential: the
    browser redirect itself carries no Authorization header, so it cannot
    prove the caller holds an API key; possession of the state can.
    """
    try:
        record = app.complete_authorization(body.installation_id, body.state)
    except GitHubAppError as exc:
        raise _github_app_error(exc) from exc
    return {"installation": record.public()}


@router.post("/github/app/sync")
def github_app_sync(
    key: ApiKey = Depends(admin_key),
    app: Any = Depends(get_github_app),
) -> dict[str, Any]:
    """Refresh installation metadata from GitHub (drops deleted installs)."""
    try:
        return {"installations": [r.public() for r in app.sync()]}
    except GitHubAppError as exc:
        raise _github_app_error(exc) from exc


@router.delete("/github/app/installations/{installation_id}")
def github_app_revoke(
    installation_id: int,
    key: ApiKey = Depends(admin_key),
    app: Any = Depends(get_github_app),
) -> dict[str, Any]:
    """Revoke one installation: best-effort delete on GitHub, always forgets
    the local record and any cached tokens. Re-run authorize to reconnect."""
    try:
        return app.revoke(installation_id)
    except GitHubAppError as exc:
        raise _github_app_error(exc) from exc


def _github_app_error(exc: GitHubAppError) -> V1ApiError:
    return V1ApiError(exc.status_code, exc.code, exc.message)
