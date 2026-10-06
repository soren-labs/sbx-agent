"""Runtime evidence ingestion: source dedupe, contiguous ack, synchronous projections
and the single terminal validation path (RFC 04 runtime source dedupe, A08/A09)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from control.application.sessions import finish_turn
from control.domain.digests import digest_of
from control.domain.errors import DomainError
from control.domain.execution import LIVE_EXECUTION_STATES
from control.domain.ids import new_id
from control.security.redaction import redact

_PART_EVENTS = ("message.part_added", "message.part_updated")
_TOOL_EVENTS = ("tool.started", "tool.updated", "tool.completed")
_REASON_BY_CODE = {
    "credential_invalid": "credential_invalid",
    "context_mismatch": "context_mismatch",
    "context_unavailable": "context_unavailable",
    "rate_limited": "provider_failed",
    "provider_failed": "provider_failed",
    "spawn_failed": "runtime_incompatible",
    "unsupported_capability": "unsupported_capability",
    "output_contract_invalid": "output_contract_invalid",
}


@dataclass
class IngestHooks:
    credential_health: Callable[[Any, dict[str, Any], dict[str, Any], str], None] | None = None
    turn_terminal: list[Callable[[Any, dict[str, Any], dict[str, Any], str], None]] = field(
        default_factory=list
    )


@dataclass
class IngestResult:
    acked: int
    terminal: bool
    applied: int


def _observed(item: dict[str, Any]) -> datetime | None:
    value = item.get("observed_at")
    return datetime.fromtimestamp(float(value), UTC) if value else None


def ingest(
    uow: Any, lease: dict[str, Any], epoch: str, items: list[dict[str, Any]], hooks: IngestHooks
) -> IngestResult:
    key = {"executor_lease_id": lease["id"], "runtime_epoch": epoch}
    offset = uow.find_one("runtime_ingestion_offsets", key, lock=True)
    if offset is None:
        offset = uow.insert("runtime_ingestion_offsets", key)
    acked = int(offset["acked_local_seq"])
    terminal = False
    applied = 0
    for item in sorted(items, key=lambda i: int(i["local_seq"])):
        seq = int(item["local_seq"])
        payload = redact(item.get("payload") or {})
        if seq <= acked:
            existing = uow.find_one("session_events", {**key, "local_seq": seq})
            if existing is not None and _strip(existing["payload"]) != _strip(payload):
                raise DomainError(
                    "validation_failed",
                    "conflicting runtime evidence for an acknowledged source tuple",
                    details={"local_seq": seq},
                )
            continue
        if seq != acked + 1:
            break  # only contiguous committed evidence is acknowledged
        terminal = _apply(uow, lease, epoch, seq, item, payload, hooks) or terminal
        acked = seq
        applied += 1
    uow.update_where(
        "runtime_ingestion_offsets", key, {"acked_local_seq": acked, "updated_at": uow.now()}
    )
    return IngestResult(acked=acked, terminal=terminal, applied=applied)


def _strip(payload: dict[str, Any]) -> str:
    return digest_of({k: v for k, v in payload.items() if k not in ("message_id", "late")})


def _apply(
    uow: Any,
    lease: dict[str, Any],
    epoch: str,
    seq: int,
    item: dict[str, Any],
    payload: dict[str, Any],
    hooks: IngestHooks,
) -> bool:
    execution = uow.get("executions", item.get("execution_id") or "", lock=True)
    if execution is None or execution["executor_lease_id"] != lease["id"]:
        raise DomainError("forbidden", "runtime evidence for an execution outside this lease")
    session = uow.get("sessions", execution["session_id"], lock=True)
    turn = uow.get("turns", execution["turn_id"], lock=True)
    kind = item["type"]
    common = {
        "actor": "runtime",
        "source": "runtime",
        "turn_id": turn["id"],
        "execution_id": execution["id"],
        "executor_lease_id": lease["id"],
        "lease_generation": lease["generation"],
        "runtime_epoch": epoch,
        "local_seq": seq,
        "observed_at": _observed(item),
    }
    if execution["state"] not in LIVE_EXECUTION_STATES:
        # Late evidence from a sealed attempt is retained as history only.
        uow.append_event(session, kind, {**payload, "late": True}, **common)
        return False
    if kind in _PART_EVENTS or kind in _TOOL_EVENTS:
        message = _output_message(uow, session, turn)
        payload = {**payload, "message_id": message["id"]}
        _upsert_part(uow, session, message, kind, payload)
        uow.append_event(session, kind, payload, **common)
        return False
    uow.append_event(session, kind, payload, **common)
    if kind == "execution.started":
        if execution["state"] == "preparing":
            uow.update(
                "executions",
                execution["id"],
                {
                    "state": "started",
                    "launch_evidence": "started",
                    "started_at": uow.now(),
                    "cli_version": payload.get("cli_version"),
                    "adapter_version": payload.get("adapter_version"),
                },
            )
        if turn["state"] == "preparing":
            uow.update(
                "turns",
                turn["id"],
                {"state": "running", "reason": None, "started_at": uow.now()},
                bump_version=True,
            )
            uow.append_event(
                session,
                "turn.started",
                {"execution_id": execution["id"]},
                actor="application",
                turn_id=turn["id"],
                execution_id=execution["id"],
            )
    elif kind == "execution.native_bound":
        _bind_native(uow, session, uow.get("executions", execution["id"]), payload)
    elif kind == "usage.observed":
        uow.update("turns", turn["id"], {"usage": payload})
    elif kind == "diagnostic.reported":
        if payload.get("credential_health") == "invalid" and hooks.credential_health:
            hooks.credential_health(uow, session, execution, "invalid")
    elif kind == "execution.observed_terminal":
        uow.update("executions", execution["id"], {"outcome": payload})
        health = payload.get("credential_health")
        if health in ("invalid", "rate_limited") and hooks.credential_health:
            hooks.credential_health(uow, session, execution, health)
    elif kind == "execution.stopped":
        terminalize(uow, session, turn, uow.get("executions", execution["id"]), payload, seq, hooks)
        return True
    return False


def _output_message(uow: Any, session: dict[str, Any], turn: dict[str, Any]) -> dict[str, Any]:
    if turn["output_message_id"]:
        return uow.get("messages", turn["output_message_id"])
    ordinal = uow.allocate(session["id"], "next_message_ordinal")
    message = uow.insert(
        "messages",
        {
            "id": new_id("message"),
            "workspace_id": session["workspace_id"],
            "session_id": session["id"],
            "ordinal": ordinal,
            "author_kind": "agent",
            "author_id": session["harness_provider"],
            "role": "assistant",
            "routing": "output",
            "content": [],
            "content_digest": digest_of([]),
            "turn_id": turn["id"],
            "state": "streaming",
        },
    )
    uow.update("turns", turn["id"], {"output_message_id": message["id"]})
    turn["output_message_id"] = message["id"]
    return message


def _upsert_part(
    uow: Any, session: dict[str, Any], message: dict[str, Any], kind: str, payload: dict[str, Any]
) -> None:
    if kind in _PART_EVENTS:
        part_key, part_kind = payload["part_key"], payload.get("kind", "text")
        content, data = str(payload.get("content") or ""), {}
        revision: int | None = int(payload.get("revision") or 1)
        mode = payload.get("mode", "replace")
    else:
        part_key, part_kind = f"tool:{payload['tool_id']}", "tool"
        content, revision, mode = "", None, "replace"
        data = {
            k: payload.get(k)
            for k in ("tool_id", "name", "status", "title", "input", "output", "error")
        }
    existing = uow.find_one(
        "message_parts", {"message_id": message["id"], "part_key": part_key}, lock=True
    )
    if existing is None:
        uow.insert(
            "message_parts",
            {
                "id": new_id("message_part"),
                "workspace_id": session["workspace_id"],
                "session_id": session["id"],
                "message_id": message["id"],
                "part_key": part_key,
                "kind": part_kind,
                "ordinal": uow.count("message_parts", {"message_id": message["id"]}),
                "revision": revision or 1,
                "content": content,
                "data": data,
            },
        )
        return
    if existing["sealed"]:
        return
    new_revision = revision if revision is not None else existing["revision"] + 1
    if new_revision <= existing["revision"]:
        return  # replayed or stale revision; never append cumulative text twice
    values: dict[str, Any] = {"revision": new_revision, "updated_at": uow.now()}
    if part_kind == "tool":
        values["data"] = {**existing["data"], **{k: v for k, v in data.items() if v is not None}}
    else:
        values["content"] = existing["content"] + content if mode == "append" else content
    uow.update("message_parts", existing["id"], values)


def _bind_native(
    uow: Any, session: dict[str, Any], execution: dict[str, Any], payload: dict[str, Any]
) -> None:
    previous = uow.find_one(
        "native_context_bindings",
        {"session_id": session["id"], "provider_id": payload.get("provider_id")},
        order="created_at DESC",
    )
    if previous is not None and previous["native_id"] == payload.get("native_id"):
        uow.update("executions", execution["id"], {"native_binding_id": previous["id"]})
        return
    binding = uow.insert(
        "native_context_bindings",
        {
            "id": new_id("native_binding"),
            "workspace_id": session["workspace_id"],
            "session_id": session["id"],
            "provider_id": payload.get("provider_id"),
            "native_id": payload.get("native_id"),
            "lineage_id": previous["lineage_id"] if previous else new_id("native_binding"),
            "cli_version": execution["cli_version"],
            "adapter_version": execution["adapter_version"],
            "execution_id": execution["id"],
        },
    )
    uow.update("executions", execution["id"], {"native_binding_id": binding["id"]})


def terminalize(
    uow: Any,
    session: dict[str, Any],
    turn: dict[str, Any],
    execution: dict[str, Any],
    stopped: dict[str, Any],
    seq: int,
    hooks: IngestHooks,
) -> None:
    """Validated execution completion -> exactly one Turn verdict (cancel precedence kept)."""
    outcome = execution["outcome"] or {}
    complete = int(stopped.get("final_local_seq") or -1) == seq and bool(execution["outcome"])
    verdict = outcome.get("verdict")
    confirmed_stop = bool(stopped.get("stopped"))
    reason = error_code = None
    message = outcome.get("message")
    if turn["state"] == "cancelling":
        turn_state = "cancelled" if confirmed_stop else "interrupted"
        exec_state = "cancelled" if confirmed_stop else "unknown"
        reason = "cancelled_by_user" if confirmed_stop else "outcome_unknown"
    elif verdict == "success" and complete and confirmed_stop:
        turn_state, exec_state = "succeeded", "succeeded"
    elif verdict == "failure":
        turn_state, exec_state = "failed", "failed"
        error_code = outcome.get("error_code") or "provider_failed"
        reason = _REASON_BY_CODE.get(error_code, "provider_failed")
    else:
        turn_state = "interrupted" if turn["state"] == "running" else "failed"
        exec_state = "unknown"
        reason = error_code = "outcome_unknown"
    if turn["state"] == "preparing" and turn_state == "succeeded":
        uow.update("turns", turn["id"], {"state": "running"})
    if execution["state"] == "preparing" and exec_state in ("succeeded",):
        uow.update("executions", execution["id"], {"state": "started"})
    if execution["state"] == "preparing" and exec_state == "cancelled":
        pass  # preparing -> cancelled is allowed directly
    if execution["state"] == "started" and exec_state == "cancelled":
        uow.update("executions", execution["id"], {"state": "stop_requested"})
    uow.update(
        "executions",
        execution["id"],
        {"state": exec_state, "finished_at": uow.now(), "final_watermark": seq},
    )
    _seal_output(uow, session, uow.get("turns", turn["id"]))
    _release_capacity(uow, execution["id"], confirmed_stop)
    finish_turn(
        uow,
        session,
        uow.get("turns", turn["id"]),
        turn_state,
        actor="application",
        reason=reason,
        error_code=error_code,
        error_message=message if turn_state != "succeeded" else None,
        evidence_complete=complete,
        outcome={
            "verdict": verdict,
            "exit_code": outcome.get("exit_code"),
            "native_id": outcome.get("native_id"),
            "credential_health": outcome.get("credential_health"),
        },
        execution_id=execution["id"],
    )
    for hook in hooks.turn_terminal:
        hook(uow, session, uow.get("turns", turn["id"]), turn_state)


def _seal_output(uow: Any, session: dict[str, Any], turn: dict[str, Any]) -> None:
    message_id = turn["output_message_id"]
    if not message_id:
        return
    uow.update_where("message_parts", {"message_id": message_id}, {"sealed": True})
    uow.update("messages", message_id, {"state": "completed", "completed_at": uow.now()})
    parts = uow.count("message_parts", {"message_id": message_id})
    uow.append_event(
        session,
        "message.completed",
        {"message_id": message_id, "parts": parts},
        actor="application",
        turn_id=turn["id"],
    )


def _release_capacity(uow: Any, execution_id: str, confirmed: bool) -> None:
    values = (
        {"state": "released", "released_at": uow.now()} if confirmed else {"state": "quarantined"}
    )
    uow.update_where(
        "capacity_reservations", {"execution_id": execution_id, "state": "active"}, values
    )
