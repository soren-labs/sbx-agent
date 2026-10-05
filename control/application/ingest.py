"""Observation → Session-event/projection reduction (RFC 167 §04 ingest).

Runtime evidence batches arrive from the ingress; within the Session row
lock we dedupe (tuple + offset), append committed events, and update typed
projections — message parts, native bindings, execution/turn state. Reads
never settle work: only the operation.result path (via
``ExecutionService.settle_operation_result``) writes terminal verdicts.
"""

from __future__ import annotations

from protocol.events import ObservationKind

from control.domain import ids
from control.domain.sessions import TurnState
from control.persistence.unit_of_work import SqlUnitOfWork

_KIND_TO_EVENT: dict[ObservationKind, str | None] = {
    ObservationKind.THREAD_STARTED: "execution.native_bound",
    ObservationKind.TURN_STARTED: "turn.started",
    ObservationKind.ITEM_STARTED: "tool.started",
    ObservationKind.ITEM_UPDATED: "tool.updated",
    ObservationKind.ITEM_COMPLETED: "tool.completed",
    ObservationKind.TOOL_OUTPUT: "tool.updated",
    ObservationKind.TURN_COMPLETED: "execution.observed_terminal",
    ObservationKind.TURN_FAILED: "execution.observed_terminal",
    ObservationKind.TURN_INTERRUPTED: "execution.observed_terminal",
    ObservationKind.PROCESS_EXITED: "execution.stopped",
    ObservationKind.DIAGNOSTIC: "diagnostic.reported",
    ObservationKind.APPROVAL_REQUESTED: "diagnostic.reported",
    ObservationKind.APPROVAL_RESOLVED: "diagnostic.reported",
    ObservationKind.NOOP: None,
}

_PART_KIND: dict[str, str] = {
    "reasoning": "reasoning",
    "agent_message": "text",
    "file_change": "tool",
    "command_execution": "tool",
}


def observations_to_records(
    observations: list[dict],
    *,
    turn_id: str,
    execution_id: str,
    lease_generation: int,
) -> list[dict]:
    """Map spool payloads → ingest_runtime_events record shape."""
    records: list[dict] = []
    for rec in observations:
        payload = rec.get("payload") or {}
        kind = payload.get("kind")
        try:
            obs_kind = ObservationKind(kind)
        except ValueError:
            obs_kind = ObservationKind.NOOP
        event_type = _KIND_TO_EVENT.get(obs_kind)
        item_type = str((payload.get("item") or {}).get("type") or "")
        if obs_kind == ObservationKind.ITEM_STARTED:
            event_type = (
                "tool.started"
                if item_type in ("file_change", "command_execution")
                else "message.part_added"
            )
        elif obs_kind == ObservationKind.ITEM_COMPLETED:
            event_type = (
                "tool.completed"
                if item_type in ("file_change", "command_execution")
                else "message.part_updated"
            )
        if event_type is None:
            continue
        records.append(
            {
                "local_seq": rec["local_seq"],
                "type": event_type,
                "payload": payload,
                "turn_id": turn_id,
                "execution_id": execution_id,
                "lease_generation": lease_generation,
                "observed_at": payload.get("observed_at"),
            }
        )
    return records


def project_observation(
    uow: SqlUnitOfWork,
    *,
    session_id: str,
    observation: dict,
    turn_id: str | None,
    execution_id: str | None,
) -> None:
    """Typed-projection updates for one committed observation payload."""
    kind = observation.get("kind")
    item = observation.get("item") or {}
    if kind == ObservationKind.ITEM_STARTED.value or kind == ObservationKind.ITEM_COMPLETED.value:
        part_id = str(item.get("id") or "")
        if not part_id:
            return
        message_id = _turn_output_message(uow, session_id, turn_id)
        if message_id is None:
            return
        status = str(item.get("status") or "")
        completed = kind == ObservationKind.ITEM_COMPLETED.value or status in (
            "completed",
            "failed",
        )
        existing = uow.rows.one(
            "SELECT ordinal FROM message_parts WHERE message_id=%s AND part_id=%s",
            (message_id, part_id),
        )
        ordinal = existing["ordinal"] if existing else _next_part_ordinal(uow, message_id)
        uow.message_parts.upsert(
            {
                "id": ids.new_id("message_part"),
                "workspace_id": uow_workspace(uow, session_id),
                "session_id": session_id,
                "message_id": message_id,
                "part_id": part_id,
                "ordinal": ordinal,
                "kind": _PART_KIND.get(str(item.get("type") or ""), "tool"),
                "revision": 1,
                "content": item,
                "sealed": completed,
                "completeness": "completed" if completed else "partial",
            },
            seal=completed,
        )
    elif kind == ObservationKind.THREAD_STARTED.value:
        native_id = observation.get("native_session_id")
        if native_id and turn_id:
            _bind_native_context(
                uow,
                session_id=session_id,
                turn_id=turn_id,
                execution_id=execution_id,
                native_id=str(native_id),
            )
    elif kind == ObservationKind.TURN_STARTED.value and turn_id:
        workspace_id = uow_workspace(uow, session_id)
        turn = uow.turns.get(workspace_id, turn_id)
        if turn is not None and turn["state"] in (
            TurnState.QUEUED.value,
            TurnState.PREPARING.value,
        ):
            uow.turns.update(
                workspace_id,
                turn_id,
                {"state": TurnState.RUNNING.value},
                expected_version=turn["version"],
            )


def uow_workspace(uow: SqlUnitOfWork, session_id: str) -> str:
    row = uow.rows.one("SELECT workspace_id FROM sessions WHERE id=%s", (session_id,))
    return row["workspace_id"]


def _turn_output_message(uow: SqlUnitOfWork, session_id: str, turn_id: str | None) -> str | None:
    if turn_id is None:
        return None
    row = uow.rows.one(
        "SELECT resolved_settings->>'output_message_id' AS mid FROM turns WHERE id=%s",
        (turn_id,),
    )
    return row["mid"] if row else None


def _next_part_ordinal(uow: SqlUnitOfWork, message_id: str) -> int:
    row = uow.rows.one(
        "SELECT COALESCE(MAX(ordinal),0)+1 AS n FROM message_parts WHERE message_id=%s",
        (message_id,),
    )
    return int(row["n"])


def _bind_native_context(
    uow: SqlUnitOfWork,
    *,
    session_id: str,
    turn_id: str,
    execution_id: str | None,
    native_id: str,
) -> None:
    workspace_id = uow_workspace(uow, session_id)
    existing = uow.rows.one(
        "SELECT id FROM native_context_bindings WHERE workspace_id=%s"
        " AND session_id=%s AND native_id=%s LIMIT 1",
        (workspace_id, session_id, native_id),
    )
    binding_id: str
    if existing is None:
        binding_id = ids.new_id("native_binding")
        uow.native_bindings.insert(
            {
                "id": binding_id,
                "workspace_id": workspace_id,
                "session_id": session_id,
                "provider_id": "opencode",
                "native_id": native_id,
                "lineage_id": native_id,
            }
        )
    else:
        binding_id = existing["id"]
    if execution_id:
        uow.executions.update(workspace_id, execution_id, {"native_binding_id": binding_id})


def settle_from_terminal_observation(
    uow: SqlUnitOfWork, *, session_id: str, turn_id: str, execution_id: str
) -> None:
    """Turn/execution state transitions on terminal observations — these are
    projections only; ``ExecutionService.settle_operation_result`` writes the
    authoritative verdict."""
    turn = uow.turns.get(uow_workspace(uow, session_id), turn_id)
    if turn is None:
        return
    if turn["state"] in (TurnState.PREPARING.value, TurnState.QUEUED.value):
        uow.turns.update(
            uow_workspace(uow, session_id),
            turn_id,
            {"state": TurnState.RUNNING.value},
            expected_version=turn["version"],
        )
