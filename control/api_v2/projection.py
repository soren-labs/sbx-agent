"""TaskRecord / run / revision → Session projections (SOR-256).

This is the anti-leak boundary: everything emitted under ``/v2`` passes
through these views, which carry product fields only. Scheduler candidates,
LRU evidence, account ids, Modal/CLI internals, artifact refs and the
internal agent/task/run id namespaces never cross it.
"""

from __future__ import annotations

import uuid
from typing import Any

from control.api_v1 import tasks as _tasks
from control.tasks import TaskRecord

SESSION_ID_PREFIX = "sess_"

"""Durable V1 aggregate → stable public session status."""
_STATUS_MAP = {
    "queued": "queued",
    "running": "running",
    "delivering": "running",
    "finished": "finished",
    "error": "failed",
    "expired": "failed",
    "delivery_failed": "failed",
    "cancelled": "cancelled",
}

"""Aggregate → UI phase. ``delivering`` keeps its own phase so the UI can
show "publishing" while status stays ``running``."""
_PHASE_MAP = {
    "queued": "queued",
    "running": "running",
    "delivering": "delivering",
    "finished": "finished",
    "error": "failed",
    "expired": "failed",
    "delivery_failed": "failed",
    "cancelled": "cancelled",
}

"""Run status → session-vocabulary status. ``UNKNOWN`` (verdict could not
be determined) reports ``failed`` — the run's ``error`` keeps the detail."""
_RUN_STATUS_MAP = {
    "CREATING": "queued",
    "QUEUED": "queued",
    "RUNNING": "running",
    "FINISHED": "finished",
    "ERROR": "failed",
    "CANCELLED": "cancelled",
    "EXPIRED": "failed",
    "UNKNOWN": "failed",
}


def new_session_id() -> str:
    return f"{SESSION_ID_PREFIX}{uuid.uuid4().hex[:16]}"


def is_session_id(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(SESSION_ID_PREFIX)


def map_session_status(aggregate: str, reason: str | None = None) -> tuple[str, str]:
    """``(status, phase)`` for a V1 aggregate status + reason."""
    status = _STATUS_MAP.get(aggregate, "failed")
    phase = (
        "provisioning"
        if aggregate == "queued" and reason == "awaiting_dispatch"
        else _PHASE_MAP.get(aggregate, "failed")
    )
    return status, phase


def map_run_status(run_status: str | None) -> str:
    return _RUN_STATUS_MAP.get(run_status or "", "failed")


def _prompt_text(record: TaskRecord) -> str:
    prompt = (record.request or {}).get("prompt") or {}
    text = prompt.get("text") if isinstance(prompt, dict) else None
    return str(text) if text else ""


def _title(record: TaskRecord) -> str | None:
    name = (record.request or {}).get("name")
    if isinstance(name, str) and name:
        return name
    text = _prompt_text(record).strip().splitlines()[0] if _prompt_text(record).strip() else ""
    return text[:80] if text else None


def _repository(record: TaskRecord) -> dict[str, Any] | None:
    resolved = (record.resolved or {}).get("source") or {}
    request = (record.request or {}).get("source") or {}
    repo = resolved.get("repo") or request.get("repo")
    if not repo:
        return None
    return {
        "repo": repo,
        "ref": resolved.get("base_ref") or request.get("ref"),
        "base_sha": resolved.get("base_sha"),
    }


def _execution(record: TaskRecord) -> dict[str, Any] | None:
    exe = (record.resolved or {}).get("execution") or {}
    out = {
        "provider": exe.get("provider"),
        "model": exe.get("model"),
        "reasoning_effort": exe.get("reasoning_effort"),
    }
    return out if any(v is not None for v in out.values()) else None


def _pull_request(pr: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(pr, dict):
        return None
    return {
        "number": pr.get("number"),
        "url": pr.get("url"),
        "state": pr.get("state"),
        "head_sha": pr.get("head_sha"),
        "head_branch": pr.get("head_branch"),
        "base": pr.get("base"),
        "draft": pr.get("draft"),
    }


def _merge(merge: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(merge, dict):
        return None
    return {
        "merged": merge.get("merged"),
        "merge_commit_sha": merge.get("merge_commit_sha"),
        "head_sha": merge.get("head_sha"),
        "merged_at": merge.get("merged_at"),
    }


def delivery_view(record: TaskRecord, ws: dict[str, Any] | None) -> dict[str, Any] | None:
    """``_delivery_view`` sanitized: required/status/branch/PR/merge/error."""
    delivery = _tasks._delivery_view(record, ws)
    if delivery is None:
        return None
    return {
        "required": delivery.get("required", False),
        "status": delivery.get("status"),
        "branch": delivery.get("branch"),
        "pushed_head_sha": delivery.get("pushed_head_sha"),
        "pull_request": _pull_request(delivery.get("pull_request")),
        "merge": _merge(delivery.get("merge")),
        "error": delivery.get("error"),
    }


def changes_view(ws: dict[str, Any] | None) -> dict[str, Any] | None:
    """``_revision_view`` + branch/PR from the durable workspace record."""
    if ws is None:
        return None
    revision = _tasks._revision_view(ws)
    if revision is None:
        return None
    status = revision["status"]
    # Uncommitted work materializes as a patch revision without moving HEAD —
    # the recorded dirty flag is the honest signal that changes exist.
    if status == "unchanged" and ws.get("dirty"):
        status = "ready"
    return {
        "status": status,
        "base_sha": revision.get("base_sha"),
        "head_sha": revision.get("head_sha"),
        "branch": ws.get("branch"),
        "pull_request": _pull_request(ws.get("pull_request")),
    }


def revision_view(public: dict[str, Any]) -> dict[str, Any]:
    """``Revision.public()`` minus the internal id namespaces/artifact ref."""
    delivery = public.get("delivery")
    return {
        "n": public.get("n"),
        "status": public.get("status"),
        "repo": public.get("repo"),
        "base_sha": public.get("base_sha"),
        "head_sha": public.get("head_sha"),
        "created_at": public.get("created_at"),
        "updated_at": public.get("updated_at"),
        "error": public.get("error"),
        "delivery": delivery_view_dict(delivery),
    }


def delivery_view_dict(delivery: dict[str, Any] | None) -> dict[str, Any] | None:
    """A revision's durable delivery record, sanitized."""
    if not isinstance(delivery, dict):
        return None
    return {
        "required": True,
        "status": delivery.get("status"),
        "branch": delivery.get("branch"),
        "pushed_head_sha": delivery.get("pushed_head_sha"),
        "pull_request": _pull_request(delivery.get("pull_request")),
        "merge": _merge(delivery),
        "error": delivery.get("error"),
    }


def _usage(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.get("input_tokens", 0),
        "cached_input_tokens": usage.get("cached_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_write_input_tokens": usage.get("cache_write_input_tokens"),
        "reasoning_output_tokens": usage.get("reasoning_output_tokens"),
    }


def _run_error(error: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(error, dict):
        return None
    return {
        "code": error.get("code"),
        "source": error.get("source"),
        "message": error.get("message"),
        "retryable": error.get("retryable"),
        "retry_after": error.get("retry_after"),
    }


def run_view(run: dict[str, Any]) -> dict[str, Any]:
    """A V1 ``_run_public`` dict → ``RunView``: status remapped, internals
    (``id``/``agent_id``/``account_id``/``artifact_refs``/``output_contract``)
    stripped."""
    prompt = run.get("prompt") or {}
    result = run.get("result") or {}
    n_raw = run.get("id") or ""
    try:
        n = int(str(n_raw).rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        n = 0
    return {
        "n": n,
        "status": map_run_status(run.get("status")),
        "prompt": prompt.get("text") if isinstance(prompt, dict) else None,
        "result": result.get("text") if isinstance(result, dict) else None,
        "error": _run_error(run.get("error")),
        "usage": _usage(run.get("usage")),
        "provider": run.get("provider"),
        "model": run.get("model"),
        "reasoning_effort": run.get("reasoning_effort"),
        "structured_output": run.get("structured_output"),
        "queue_position": run.get("queue_position"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
    }


_WS_UNSET = object()


def session_view(
    record: TaskRecord,
    *,
    plane: Any,
    task_store: Any,
    run_states: Any,
    ws: dict[str, Any] | None | object = _WS_UNSET,
    aggregate_status: str | None = None,
    aggregate_reason: str | None = None,
    error: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    cost_estimate_usd: float | None = None,
    turns: int | None = None,
) -> dict[str, Any]:
    """The sanitized Session projection.

    ``aggregate_status``/``aggregate_reason`` come from a caller that already
    paid ``_settle_task``; otherwise they are recomputed here. ``ws`` is the
    caller's prefetched workspace record (``None`` = known absent); unset
    means fetch it here.
    """
    if ws is _WS_UNSET:
        ws = _tasks._ws_record(plane, record.agent_id)
    if aggregate_status is None:
        aggregate_status, aggregate_reason = _tasks._aggregate_status(
            record, run_states, plane, ws if isinstance(ws, dict) else None
        )
    status, phase = map_session_status(aggregate_status, aggregate_reason)
    return {
        "id": record.id,
        "title": _title(record),
        "status": status,
        "phase": phase,
        "prompt": _prompt_text(record),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "repository": _repository(record),
        "execution": _execution(record),
        "turns": turns if turns is not None else 0,
        "usage": _usage(usage),
        "cost_estimate_usd": cost_estimate_usd,
        "delivery": delivery_view(record, ws),
        "changes": changes_view(ws),
        "error": _run_error(error),
    }


def live_extras(record: TaskRecord, plane: Any) -> dict[str, Any]:
    """usage/cost/turns from the live agent record (zeroes when gone)."""
    if record.agent_id is None:
        return {"usage": None, "cost_estimate_usd": None, "turns": 0}
    rec = plane.get(record.agent_id)
    if rec is None:
        return {"usage": None, "cost_estimate_usd": None, "turns": 0}
    pub = plane.public(rec)
    return {
        "usage": pub.get("usage"),
        "cost_estimate_usd": pub.get("cost_estimate_usd"),
        "turns": int(pub.get("turns") or 0),
    }


def latest_run_error(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The most recent run's error, for the session-level ``error`` field."""
    for run in reversed(runs):
        if run.get("error"):
            return run["error"]
    return None


def _session_status_for_sse(view: dict[str, Any]) -> dict[str, Any]:
    """The synthetic ``session.status`` event payload (no SSE id — replayed
    fresh on every connect so resume semantics stay line-numbered)."""
    return {
        "type": "session.status",
        "status": view["status"],
        "phase": view["phase"],
    }
