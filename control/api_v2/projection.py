"""SOR-256: Session projection — maps Task/Agent/Run truth to V2 views.

A *Session* is a durable Task record plus the live agent/run state it links
to. Every read converges through the same aggregate the V1 task surface uses
(``_settle_task``), so V2 status can never drift from V1 truth; the public
``status``/``phase`` pair is a lossy, stable-vocabulary map over it.

Status/phase derivation (V2):

    aggregate (V1)      -> status       phase
    ------------------  ------------    ---------------------------
    queued, no agent    queued          resolving      (resolution in flight)
    queued, creating    queued          provisioning   (sandbox cold start)
    queued, CREATING    queued          starting_provider (init/dispatch gap)
    queued, QUEUED      queued          queued         (parked behind work)
    queued, other       queued          queued
    running             running         running
    delivering          running         publishing
    finished            finished        finished
    error/expired/      failed          failed
      delivery_failed
    cancelled           cancelled       cancelled
"""

from __future__ import annotations

import threading
from typing import Any

from control.api_v1 import routes as _routes
from control.api_v1 import tasks as _tasks
from control.api_v1.state import V1State
from control.tasks import TaskRecord, TaskStore

# ---------------------------------------------------------------------------
# status / phase
# ---------------------------------------------------------------------------

_AGGREGATE_TO_STATUS = {
    "queued": "queued",
    "running": "running",
    "delivering": "running",
    "finished": "finished",
    "error": "failed",
    "expired": "failed",
    "delivery_failed": "failed",
    "cancelled": "cancelled",
    "failed": "failed",  # V2 resolution failure (no agent was ever bound)
}


def public_status(aggregate: str) -> str:
    """Stable public status for a V1 task aggregate."""
    return _AGGREGATE_TO_STATUS.get(aggregate, "queued")


def public_phase(
    record: TaskRecord,
    rec: Any,
    run_statuses: list[tuple[int, str]],
    aggregate: str,
) -> str:
    """Finer lifecycle position within (or beside) the public status."""
    if aggregate in ("finished", "failed", "cancelled"):
        return aggregate if aggregate in ("finished", "cancelled") else "failed"
    if aggregate == "delivering":
        return "publishing"
    if aggregate == "running":
        return "running"
    # aggregate is "queued" (or an unsettled stored status): position is
    # decided by how far the first run has come.
    if record.agent_id is None:
        return "resolving"
    if rec is None or rec.status == "creating":
        return "provisioning"
    if any(s == "CREATING" for _, s in run_statuses):
        return "starting_provider"
    return "queued"


def run_statuses(record: TaskRecord, run_states: Any, plane: Any) -> list[tuple[int, str]]:
    """``(n, run status)`` for every run of the session's agent, ledger-first."""
    return _tasks._run_statuses(record, run_states, plane)


def settle(record: TaskRecord, task_store: TaskStore, plane: Any, run_states: Any) -> Any:
    """Converge the durable record to the live aggregate (V1 seam)."""
    return _tasks._settle_task(record, task_store, plane, run_states)


# ---------------------------------------------------------------------------
# small views
# ---------------------------------------------------------------------------


def _session_record(plane: Any, record: TaskRecord) -> Any:
    if record.agent_id is None:
        return None
    return plane.get(record.agent_id)


def _execution_view(record: TaskRecord, rec: Any, v1: V1State | None) -> dict[str, Any]:
    execution = (record.resolved or {}).get("execution") or {}
    provider = execution.get("provider")
    account_id = execution.get("account_id")
    model = execution.get("model")
    effort = execution.get("reasoning_effort")
    if rec is not None:
        tags = rec.sandbox_tags or {}
        meta = _routes._meta_for(v1, rec) if v1 is not None else None
        provider = provider or tags.get("provider") or (meta.provider if meta else None)
        account_id = account_id or tags.get("account_id") or (meta.account_id if meta else None)
        model = model or rec.model
        effort = effort or rec.reasoning_effort
    if provider == "auto":
        provider = None
    if account_id in (None, "auto"):
        account_id = None
    if model == "auto":
        model = None
    if effort == "auto":
        effort = None
    return {
        "provider": provider,
        "model": model,
        "reasoning_effort": effort,
        "account_id": account_id,
    }


def _repository_view(record: TaskRecord) -> dict[str, Any] | None:
    source = ((record.resolved or {}).get("source") or {}) or (
        (record.request or {}).get("source") or (record.request or {}).get("repository") or {}
    )
    repo = source.get("repo")
    if not repo:
        return None
    return {
        "repo": repo,
        "ref": source.get("ref") or source.get("base_ref"),
        "base_sha": source.get("base_sha"),
    }


def _usage_view(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    if not usage:
        return None
    out = {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cached_input_tokens": int(usage.get("cached_input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }
    for key in ("cache_write_input_tokens", "reasoning_output_tokens"):
        if usage.get(key) is not None:
            out[key] = int(usage[key])
    return out


def _title(record: TaskRecord, rec: Any) -> str | None:
    name = (record.request or {}).get("name") or (record.request or {}).get("title")
    if name:
        return str(name)
    return rec.title if rec is not None else None


def _error_view(
    record: TaskRecord,
    plane: Any,
    run_states: Any,
    aggregate: str,
) -> dict[str, Any] | None:
    """The session's failure detail, from the failing run or the resolution."""
    if aggregate not in ("error", "expired", "delivery_failed", "failed"):
        return None
    if record.agent_id is not None:
        ledger = _routes._ledger(plane)
        if ledger is not None:
            try:
                for run in reversed(ledger.list(record.agent_id)):
                    if run.error:
                        out = dict(run.error)
                        out.setdefault("retryable", bool(out.get("retryable")))
                        return out
            except Exception:
                pass
        ws_error = None
        ws = _tasks._ws_record(plane, record.agent_id)
        if ws and ws.get("publish_error") and aggregate == "delivery_failed":
            ws_error = ws["publish_error"]
        if ws_error:
            return {"code": "delivery_failed", "message": str(ws_error), "retryable": True}
    # Resolution-time failure: persisted on the transition log.
    for transition in reversed(record.transitions or []):
        if transition.get("status") in ("failed", "error"):
            return {
                "code": str(transition.get("reason") or "internal"),
                "message": str(transition.get("detail") or transition.get("reason") or ""),
                "retryable": bool(transition.get("retryable")),
            }
    return None


def _message_view(index: int, message: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"msg-{index}",
        "role": "assistant" if message.get("role") == "assistant" else "user",
        "text": str(message.get("text") or ""),
        "created_at": message.get("ts"),
    }


def _recent_messages(rec: Any, limit: int) -> list[dict[str, Any]]:
    messages = list(rec.messages or []) if rec is not None else []
    tail = list(enumerate(messages, start=1))[-limit:]
    return [_message_view(i, m) for i, m in tail]


def _activity_view(entry_id: int, event: dict[str, Any]) -> dict[str, Any] | None:
    """Map a transcript/canonical event entry to a public activity row."""
    etype = event.get("type")
    if etype not in ("item.started", "item.updated", "item.completed", "error"):
        return None
    if etype == "error":
        return {
            "id": f"act-{entry_id}",
            "kind": "error",
            "status": "failed",
            "message": str(event.get("message") or ""),
        }
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    kind = str(item.get("type") or "unknown")
    status = {
        "item.started": "running",
        "item.updated": "running",
        "item.completed": str(item.get("status") or "completed"),
    }[etype]
    out: dict[str, Any] = {
        "id": str(item.get("id") or f"act-{entry_id}"),
        "kind": kind,
        "status": status,
    }
    if kind == "command_execution":
        out["command"] = item.get("command")
        if item.get("exit_code") is not None:
            out["exit_code"] = item.get("exit_code")
        out["summary"] = item.get("command")
    elif kind == "file_change":
        changes = item.get("changes")
        path = None
        if isinstance(changes, list) and changes:
            first = changes[0]
            if isinstance(first, dict):
                path = first.get("path")
        out["path"] = path
        out["summary"] = path
    elif kind in ("agent_message", "reasoning"):
        text = str(item.get("text") or "")
        out["text"] = text[:2000] if len(text) > 2000 else text or None
        out["summary"] = out["text"]
    elif kind == "error":
        out["message"] = item.get("message")
        out["summary"] = item.get("message")
    else:
        out["summary"] = kind
    return out


def _recent_activities(
    plane: Any,
    record: TaskRecord,
    statuses: list[tuple[int, str]],
    limit: int,
) -> list[dict[str, Any]]:
    """Latest run's persisted activity transcript, bounded — no sandbox read."""
    store = getattr(plane, "run_activity", None)
    if store is None or record.agent_id is None or not statuses:
        return []
    for n, _status in reversed(sorted(statuses)):
        try:
            entries = store.get(record.agent_id, n)
        except Exception:
            entries = None
        if entries:
            out = [v for v in (_activity_view(e["id"], e["event"]) for e in entries) if v]
            return out[-limit:]
    return []


def _delivery_mode(record: TaskRecord) -> str | None:
    """The declared delivery mode (request vocabulary), if any."""
    delivery = (record.request or {}).get("delivery") or {}
    mode = delivery.get("mode") if isinstance(delivery, dict) else None
    return str(mode) if mode else None


def delivery_view(
    record: TaskRecord,
    ws: dict[str, Any] | None,
    revisions: list[Any],
) -> dict[str, Any] | None:
    """Session delivery: pending/delivered/failed + PR block; None = no policy."""
    delivery = _tasks._delivery_view(record, ws)
    if delivery is None:
        # A deliver could still have run through the revision surface (or the
        # task declared mode=none): reflect the latest revision's record.
        latest = revisions[-1] if revisions else None
        if latest is not None and latest.delivery:
            delivery = dict(latest.delivery)
        else:
            return None
    out: dict[str, Any] = {"status": delivery.get("status") or "pending"}
    mode = _delivery_mode(record)
    if mode:
        out["mode"] = mode
    if delivery.get("branch"):
        out["branch"] = delivery["branch"]
    if delivery.get("pushed_head_sha"):
        out["pushed_head_sha"] = delivery["pushed_head_sha"]
    pr = delivery.get("pull_request")
    if isinstance(pr, dict):
        out["pull_request"] = {
            "url": pr.get("url"),
            "number": pr.get("number"),
            "state": pr.get("state"),
            "draft": pr.get("draft"),
            "base": pr.get("base"),
        }
    merge = delivery.get("merge")
    if isinstance(merge, dict) and merge.get("merged"):
        out["status"] = "delivered"
        if merge.get("merge_commit_sha"):
            out["pushed_head_sha"] = merge["merge_commit_sha"]
    err = delivery.get("error")
    if isinstance(err, dict):
        out["error"] = str(err.get("message") or err.get("code") or "")
    elif err:
        out["error"] = str(err)
    if out["status"] not in ("none", "pending", "delivered", "failed"):
        out["status"] = "pending"
    return out


def changes_view(revisions: list[Any], ws: dict[str, Any] | None) -> dict[str, Any]:
    """Session-level changes summary — Revision ids stay internal."""
    latest = revisions[-1] if revisions else None
    head = (ws or {}).get("head_sha") or (latest.head_sha if latest else None)
    base = (ws or {}).get("base_sha") or (latest.base_sha if latest else None)
    if latest is not None and latest.status != "ready":
        status = "materialization_failed"
    elif head is None:
        status = "none"
    elif head == base:
        status = "unchanged"
    else:
        status = "ready"
    rows: list[dict[str, Any]] = []
    for rev in revisions:
        error = None
        if isinstance(rev.error, dict):
            error = str(rev.error.get("message") or rev.error.get("code") or "")
        rows.append(
            {
                "id": rev.revision_id,
                "n": rev.n,
                "status": rev.status,
                "base_sha": rev.base_sha or None,
                "head_sha": rev.head_sha or None,
                "created_at": rev.created_at or None,
                "error": error,
            }
        )
    return {
        "status": status,
        "repo": (latest.repo if latest else None) or ((ws or {}).get("repo") or None),
        "base_sha": base,
        "head_sha": head,
        "count": len(rows),
        "revisions": rows,
    }


# ---------------------------------------------------------------------------
# summary / detail
# ---------------------------------------------------------------------------


def summary_view(
    record: TaskRecord,
    *,
    plane: Any,
    v1: V1State | None,
    run_states: Any,
    ws: dict[str, Any] | None,
    aggregate: str | None = None,
) -> dict[str, Any]:
    rec = _session_record(plane, record)
    statuses = run_statuses(record, run_states, plane)
    if aggregate is None:
        aggregate = record.status
    usage = _usage_view(rec.usage if rec is not None else None)
    return {
        "id": record.id,
        "title": _title(record, rec),
        "status": public_status(aggregate),
        "phase": public_phase(record, rec, statuses, aggregate),
        "execution": _execution_view(record, rec, v1),
        "repository": _repository_view(record),
        "usage": usage,
        "error": _error_view(record, plane, run_states, aggregate),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def detail_view(
    record: TaskRecord,
    *,
    plane: Any,
    v1: V1State | None,
    run_states: Any,
    revisions: Any,
    ws: dict[str, Any] | None,
    aggregate: str,
    message_limit: int = 50,
    activity_limit: int = 50,
) -> dict[str, Any]:
    """Bounded first-view projection: point reads only, no sandbox exec."""
    out = summary_view(
        record, plane=plane, v1=v1, run_states=run_states, ws=ws, aggregate=aggregate
    )
    rec = _session_record(plane, record)
    statuses = run_statuses(record, run_states, plane)
    out["prompt"] = ((record.request or {}).get("prompt") or {}).get("text")
    out["messages"] = _recent_messages(rec, message_limit)
    out["activities"] = _recent_activities(plane, record, statuses, activity_limit)
    revs = []
    if record.agent_id is not None and revisions is not None:
        try:
            revs = revisions.list(record.agent_id)
        except Exception:
            revs = []
    out["changes"] = changes_view(revs, ws)
    out["delivery"] = delivery_view(record, ws, revs)
    if rec is not None:
        pub = plane.public(rec)
        out["cost_estimate_usd"] = pub.get("cost_estimate_usd", 0.0)
    return out


# ---------------------------------------------------------------------------
# deferred mutation executor (ACK quickly; durable intent runs on a thread)
# ---------------------------------------------------------------------------

_DEFAULT_ACK_BUDGET_S = 0.75


def run_with_budget(fn: Any, timeout: float) -> tuple[bool, dict[str, Any]]:
    """Run ``fn`` on a daemon thread; ``(completed, box)`` after ``timeout``.

    ``box`` holds ``result``/``error`` once the worker settles — on a deferred
    (``completed=False``) call the box keeps filling in the background, so
    callers that *only* ACK never lose the work.
    """
    box: dict[str, Any] = {}
    done = threading.Event()

    def _work() -> None:
        try:
            box["result"] = fn()
        except Exception as exc:
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_work, daemon=True, name="sbx-v2-mutation").start()
    if done.wait(timeout):
        return True, box
    return False, box


def ack_budget_s(app_state: Any) -> float:
    return float(getattr(app_state, "v2_ack_budget_s", _DEFAULT_ACK_BUDGET_S))
