"""The eleven SBX MCP tools: argument models, honest annotations and SDK-only handlers.

Every handler calls the public SDK (sbx.sdk) and nothing else: no database, no internal
gateway, no business rules beyond the explicit-selection checks in `CreateTaskArgs`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from mcp import types
from pydantic import BaseModel, ConfigDict, Field, model_validator

from sbx.sdk import SBXClient, SBXError
from sbx.sdk.client import TERMINAL_TURN

MAX_RESULT_BYTES = 64 * 1024
MAX_MODELS_PER_CONNECTION = 50

Id = Annotated[str, Field(min_length=1, max_length=64)]
IdempotencyKey = Annotated[str, Field(min_length=8, max_length=200)]
Text = Annotated[str, Field(min_length=1, max_length=20000)]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(_Args):
    pass


class ListResourcesArgs(_Args):
    harness_id: Id | None = Field(
        None, description="Limit to one harness; also returns that harness's model lists."
    )
    probe: bool = Field(
        False,
        description="Run a live, bounded reachability check on resident machines. Default "
        "reports the last cached status only.",
    )


class ListTasksArgs(_Args):
    lifecycle: list[Literal["open", "archived", "closed"]] | None = Field(None, max_length=3)
    limit: int = Field(20, ge=1, le=50)
    cursor: str | None = Field(None, max_length=512)
    q: str | None = Field(None, max_length=200, description="Title or exact id search.")


class CreateTaskArgs(_Args):
    prompt: Text
    harness_id: Id = Field(description="Harness provider id, e.g. opencode, codex, claude.")
    model: str | None = Field(None, min_length=1, max_length=200)
    effort: str | None = Field(None, min_length=1, max_length=32)
    executor_backend: Literal["local", "modal", "resident"] = Field(
        description="Where the work runs. resident needs resident_machine_id and is offered only "
        "when sbx_list_resources reports it."
    )
    resident_machine_id: Id | None = Field(
        None, description="Required for executor_backend resident: the machine (rmach_…)."
    )
    inference_mode: Literal["custom_api", "subscription"] = Field(
        description="custom_api spends an API-key Connection; subscription uses a Machine Slot."
    )
    inference_connection_id: Id | None = Field(
        None, description="Required for custom_api; the API-key Connection to use."
    )
    machine_slot_id: Id | None = Field(
        None, description="Required for subscription; the Machine Slot to use (Modal only)."
    )
    project_id: Id | None = None
    idempotency_key: IdempotencyKey = Field(
        description="Reuse the same key when retrying the same request; never reuse it otherwise."
    )

    @model_validator(mode="after")
    def explicit_selection(self) -> CreateTaskArgs:
        # Billing intent is never inferred: each mode needs its own resource and no other.
        if self.executor_backend == "resident":
            if not self.resident_machine_id:
                raise ValueError("executor_backend resident needs resident_machine_id")
        elif self.resident_machine_id:
            raise ValueError("resident_machine_id is only valid with executor_backend resident")
        if self.inference_mode == "custom_api":
            if not self.inference_connection_id:
                raise ValueError("custom_api needs inference_connection_id (no automatic choice)")
            if self.machine_slot_id:
                raise ValueError("custom_api must not set machine_slot_id")
        else:
            if not self.machine_slot_id:
                raise ValueError("subscription needs machine_slot_id (no fallback to an API key)")
            if self.inference_connection_id:
                raise ValueError("subscription must not set inference_connection_id")
            if self.executor_backend != "modal":
                raise ValueError("subscription Sessions run on executor_backend modal")
        return self


class GetTaskArgs(_Args):
    session_id: Id
    turn_id: Id | None = Field(None, description="Also return this Turn's state.")


class GetEventsArgs(_Args):
    session_id: Id
    after: int | None = Field(None, ge=0, description="Forward page: events with seq > after.")
    before: int | None = Field(None, ge=1, description="History page: events below this seq.")
    limit: int = Field(100, ge=1, le=200)
    turn_id: Id | None = None
    types: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        None, max_length=20
    )

    @model_validator(mode="after")
    def one_cursor(self) -> GetEventsArgs:
        if self.after is not None and self.before is not None:
            raise ValueError("pass after or before, not both")
        return self


class GetResultArgs(_Args):
    turn_id: Id
    max_chars: int = Field(16000, ge=1, le=32000)
    offset: int = Field(0, ge=0)


class FollowupArgs(_Args):
    session_id: Id
    content: Text
    idempotency_key: IdempotencyKey


class TurnKeyArgs(_Args):
    turn_id: Id
    idempotency_key: IdempotencyKey


class TurnArgs(_Args):
    turn_id: Id


Handler = Callable[[SBXClient, Any], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[_Args]
    annotations: types.ToolAnnotations
    output: dict[str, Any]
    handler: Handler

    def describe(self) -> types.Tool:
        return types.Tool(
            name=self.name,
            description=self.description,
            input_schema=self.args.model_json_schema(),
            output_schema=self.output,
            annotations=self.annotations,
        )


class OutputTooLarge(Exception):
    pass


def bounded(payload: dict[str, Any]) -> dict[str, Any]:
    if len(json.dumps(payload, default=str)) > MAX_RESULT_BYTES:
        raise OutputTooLarge("the result exceeds the 64 KiB limit")
    return payload


def _hints(
    read_only: bool = True, *, destructive: bool = False, open_world: bool = False
) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=True,
        open_world_hint=open_world,
    )


def _object(required: list[str], **props: Any) -> dict[str, Any]:
    return {"type": "object", "required": required, "properties": props}


TERMINAL = set(TERMINAL_TURN)


def _turn_summary(turn: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": turn["id"],
        "session_id": turn["session_id"],
        "state": turn["state"],
        "reason": turn.get("reason"),
        "terminal": turn["state"] in TERMINAL,
        "cancel_requested": bool(turn.get("cancel_requested")),
        # Cancellation is only "pending" until the API reports a terminal state.
        "cancel_pending": bool(turn.get("cancel_requested")) and turn["state"] not in TERMINAL,
        "actions": turn.get("actions", []),
        "error": turn.get("error"),
    }


# -- read tools -------------------------------------------------------------------------


def list_harnesses(c: SBXClient, _a: NoArgs) -> dict[str, Any]:
    items = []
    for harness in c.harnesses():
        caps = {
            name: (entry.get("status") if isinstance(entry, dict) else entry)
            for name, entry in (harness.get("capabilities") or {}).items()
        }
        items.append(
            {
                "provider_id": harness["provider_id"],
                "support_tier": harness.get("support_tier"),
                "startable": harness.get("support_tier") == "supported",
                "adapter_version": harness.get("adapter_version"),
                "cli_version": harness.get("cli_version"),
                "inference_protocols": harness.get("inference_protocols", []),
                "capabilities": caps,
            }
        )
    return bounded({"items": items})


def list_resources(c: SBXClient, a: ListResourcesArgs) -> dict[str, Any]:
    harnesses = [h for h in c.harnesses() if a.harness_id in (None, h["provider_id"])]
    if a.harness_id and not harnesses:
        raise SBXError("not_found", "harness not found", status=404)
    slots = c.slots.list()
    out = []
    for harness in harnesses:
        provider = harness["provider_id"]
        supported = harness.get("support_tier") == "supported"
        api_routes: list[dict[str, Any]] = []
        slot_routes: list[dict[str, Any]] = []
        if supported:
            for con in c.models(provider_id=provider).get("connections", []):
                compatible = bool(con.get("compatible"))
                route = {
                    "connection_id": con["connection_id"],
                    "label": con.get("label"),
                    "health": con.get("health"),
                    "compatible": compatible,
                    # Usable only when the API says it speaks this harness and is healthy.
                    "usable": compatible and con.get("health") == "ready",
                }
                if a.harness_id:
                    names = [m["id"] for m in con.get("models", [])]
                    route["models"] = names[:MAX_MODELS_PER_CONNECTION]
                    route["models_truncated"] = len(names) > MAX_MODELS_PER_CONNECTION
                api_routes.append(route)
            for slot in slots:
                if slot["provider"] != provider:
                    continue
                route = {
                    "machine_slot_id": slot["id"],
                    "label": slot.get("label"),
                    "status": slot["status"],
                    "usable": slot["status"] == "ready",
                }
                if a.harness_id and route["usable"]:
                    names = [m["id"] for m in c.slots.models(slot["id"]).get("models", [])]
                    route["models"] = names[:MAX_MODELS_PER_CONNECTION]
                slot_routes.append(route)
        out.append(
            {
                "provider_id": provider,
                "support_tier": harness.get("support_tier"),
                "startable": supported,
                "api_key_routes": api_routes,
                "subscription_routes": slot_routes,
            }
        )
    backends = c.executor_backends()
    resident = "resident" in advertised_backends(backends)
    return bounded(
        {
            "harnesses": out,
            "executor_backends": [dict(backend) for backend in backends],
            "resident_machines": resident_machines(c, probe=a.probe) if resident else [],
            # Resident routes are called only when the deployment advertises the backend.
            "resident_executor": "advertised" if resident else "not_enabled",
        }
    )


def advertised_backends(backends: list[dict[str, Any]]) -> set[str]:
    return {str(b.get("kind") or b.get("backend")) for b in backends}


def resident_machines(c: SBXClient, *, probe: bool) -> list[dict[str, Any]]:
    """Resident machines of the workspace, projected to what a caller may act on.

    Called only after the deployment advertises the resident backend.
    """
    page = c.resident_machines.list(probe=probe)
    items = []
    for machine in page["items"]:
        reach = machine.get("reachability") or {}
        items.append(
            {
                "id": machine["id"],
                "name": machine.get("name"),
                "state": machine.get("state"),
                "capacity": machine.get("capacity"),
                "reachability": {
                    "status": reach.get("status"),
                    "reason": reach.get("reason"),
                    "checked_at": reach.get("checked_at"),
                },
                # Usable only when the machine is active and its last check says reachable.
                "usable": machine.get("state") == "active" and reach.get("status") == "reachable",
            }
        )
    return items


def list_tasks(c: SBXClient, a: ListTasksArgs) -> dict[str, Any]:
    page = c.sessions.list(
        lifecycle=",".join(a.lifecycle) if a.lifecycle else None,
        limit=a.limit,
        cursor=a.cursor,
        q=a.q,
    )
    return bounded({"items": page["items"], "next_cursor": page.get("next_cursor")})


def get_task(c: SBXClient, a: GetTaskArgs) -> dict[str, Any]:
    session = c.sessions.get(a.session_id)["session"]
    out: dict[str, Any] = {
        "session": session,
        "active_turn_id": session.get("active_turn_id"),
        "turn": None,
    }
    if a.turn_id:
        turn = c.turns.get(a.turn_id)
        if turn["session_id"] != a.session_id:
            raise SBXError("not_found", "turn not found in this session", status=404)
        out["turn"] = _turn_summary(turn)
    return bounded(out)


def get_events(c: SBXClient, a: GetEventsArgs) -> dict[str, Any]:
    page = c.sessions.events_page(
        a.session_id,
        after=a.after,
        before=a.before,
        limit=a.limit,
        types=a.types,
        turn_id=a.turn_id,
    )
    items = page["items"]
    return bounded(
        {
            "items": items,
            "next_after": page.get("next_after"),
            "next_before": page.get("next_before"),
            # A full page may have more behind it; an empty next page means the end.
            "has_more": len(items) >= a.limit,
            "event_watermark": page.get("event_watermark"),
        }
    )


def get_result(c: SBXClient, a: GetResultArgs) -> dict[str, Any]:
    turn = c.turns.get(a.turn_id)
    summary = _turn_summary(turn)
    if turn["state"] not in TERMINAL or not turn.get("output_message_id"):
        return bounded(
            {
                "turn": summary,
                "output_message_id": turn.get("output_message_id"),
                "text": None,
                "offset": a.offset,
                "next_offset": None,
                "total_chars": 0,
            }
        )
    # Read only this Turn's messages (additive ``turn_id`` filter), never the whole Session.
    messages = c.messages.list(turn["session_id"], turn_id=a.turn_id)
    output = next((m for m in messages if m["id"] == turn["output_message_id"]), None)
    text = (
        "".join(p["content"] for p in output.get("parts", []) if p.get("kind") == "text")
        if output
        else ""
    )
    window = text[a.offset : a.offset + a.max_chars]
    more = a.offset + a.max_chars < len(text)
    return bounded(
        {
            "turn": summary,
            "output_message_id": turn["output_message_id"],
            "text": window,
            "offset": a.offset,
            "next_offset": a.offset + a.max_chars if more else None,
            "total_chars": len(text),
        }
    )


# -- side-effecting tools ---------------------------------------------------------------


def create_task(c: SBXClient, a: CreateTaskArgs) -> dict[str, Any]:
    body: dict[str, Any] = {
        "message": {"content": a.prompt},
        "harness": {
            "provider_id": a.harness_id,
            **({"model": a.model} if a.model else {}),
            **({"effort": a.effort} if a.effort else {}),
        },
        "executor": {"backend": a.executor_backend},
    }
    if a.executor_backend == "resident":
        if "resident" not in advertised_backends(c.executor_backends()):
            raise SBXError(
                "validation_failed",
                "the resident executor is not enabled on this deployment",
                details={"field": "executor_backend", "value": "resident"},
            )
        body["executor"]["resident_machine_id"] = a.resident_machine_id
    if a.project_id:
        body["project_id"] = a.project_id
    if a.inference_mode == "subscription":
        body["inference"] = {"mode": "subscription", "machine_slot_id": a.machine_slot_id}
    else:
        body["inference"] = {"mode": "custom_api"}
        body["connections"] = {"inference": a.inference_connection_id}
    accepted = c.sessions.create(idempotency_key=a.idempotency_key, **body)
    return bounded(
        {
            "session_id": accepted["session_id"],
            "turn_id": accepted.get("turn_id"),
            "event_watermark": accepted.get("event_watermark"),
            "inference_mode": a.inference_mode,
        }
    )


def send_followup(c: SBXClient, a: FollowupArgs) -> dict[str, Any]:
    accepted = c.sessions.send(a.session_id, a.content, idempotency_key=a.idempotency_key)
    return bounded(
        {
            "session_id": a.session_id,
            "turn_id": accepted.get("turn_id"),
            "event_watermark": accepted.get("event_watermark"),
        }
    )


def cancel_task(c: SBXClient, a: TurnKeyArgs) -> dict[str, Any]:
    body = c.turns.cancel(a.turn_id, idempotency_key=a.idempotency_key)
    return bounded(
        {"turn": _turn_summary(body["turn"]), "event_watermark": body.get("event_watermark")}
    )


def retry_task(c: SBXClient, a: TurnKeyArgs) -> dict[str, Any]:
    body = c.turns.retry(a.turn_id, idempotency_key=a.idempotency_key)
    turn = body.get("turn", body)
    return bounded({"turn": _turn_summary(turn), "event_watermark": body.get("event_watermark")})


def acknowledge_unknown(c: SBXClient, a: TurnArgs) -> dict[str, Any]:
    body = c.turns.acknowledge(a.turn_id)
    turn = body.get("turn", body)
    return bounded({"turn": _turn_summary(turn), "event_watermark": body.get("event_watermark")})


# -- registry ---------------------------------------------------------------------------

_TURN_OUT = _object(["turn"], turn=_object(["id", "state", "terminal", "cancel_pending"]))
_PAGE_OUT = _object(["items"], next_cursor={"type": ["string", "null"]})

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "sbx_list_harnesses",
        "List official CLI harnesses, their support tier and capabilities. Read-only.",
        NoArgs,
        _hints(),
        _object(["items"], items={"type": "array"}),
        list_harnesses,
    ),
    ToolSpec(
        "sbx_list_resources",
        "List, per harness, the API-key Connections and Machine Slots that can run it, with a "
        "usable flag the API reports, the enabled executor backends and resident machines. "
        "probe=true runs a live, bounded reachability check that reaches your machines. Pass "
        "harness_id to also get model lists. Never returns credentials.",
        ListResourcesArgs,
        _hints(open_world=True),
        _object(["harnesses", "executor_backends"]),
        list_resources,
    ),
    ToolSpec(
        "sbx_list_tasks",
        "List Sessions (durable tasks) in this workspace, newest first, in pages. Pass "
        "next_cursor back as cursor for the next page. Read-only.",
        ListTasksArgs,
        _hints(),
        _PAGE_OUT,
        list_tasks,
    ),
    ToolSpec(
        "sbx_create_task",
        "Create a Session and start its first Turn; returns at once with ids, not the result. "
        "The task may run commands and change files in its workspace, and it may spend an "
        "API-key Connection or a Machine Slot. Choose inference_mode explicitly: custom_api "
        "needs inference_connection_id, subscription needs machine_slot_id and "
        "executor_backend modal. Reuse idempotency_key only to retry the same request.",
        CreateTaskArgs,
        _hints(False, destructive=True, open_world=True),
        _object(["session_id", "turn_id"]),
        create_task,
    ),
    ToolSpec(
        "sbx_get_task",
        "Read a Session's summary and active_turn_id; with turn_id, also that Turn's state. "
        "Returns the current state without waiting. Read-only.",
        GetTaskArgs,
        _hints(),
        _object(["session", "active_turn_id"]),
        get_task,
    ),
    ToolSpec(
        "sbx_get_events",
        "Read one bounded page of a Session's committed events, forward with after or back "
        "with before, optionally for one Turn. Read-only.",
        GetEventsArgs,
        _hints(),
        _object(["items", "has_more"], items={"type": "array"}),
        get_events,
    ),
    ToolSpec(
        "sbx_get_result",
        "Read the output text of one Turn, in windows of max_chars from offset. Only that "
        "Turn's output is returned; text is null until the Turn is terminal. Read-only.",
        GetResultArgs,
        _hints(),
        _object(["turn", "text"]),
        get_result,
    ),
    ToolSpec(
        "sbx_send_followup",
        "Queue a follow-up message on a Session; it runs as a new Turn after the current work. "
        "The new work can run commands and change files. Reuse idempotency_key only to retry "
        "the same message.",
        FollowupArgs,
        _hints(False, destructive=True, open_world=True),
        _object(["session_id", "turn_id"]),
        send_followup,
    ),
    ToolSpec(
        "sbx_cancel_task",
        "Request cancellation of a Turn. The result is pending (cancel_pending) until the API "
        "reports a terminal state; partial file changes may remain. Reuse idempotency_key only "
        "to retry the same request.",
        TurnKeyArgs,
        _hints(False, destructive=True),
        _TURN_OUT,
        cancel_task,
    ),
    ToolSpec(
        "sbx_retry_task",
        "Start a new Turn that retries a failed, cancelled or interrupted Turn. Allowed only "
        "when the Turn's actions include retry. Reuse idempotency_key only to retry the same "
        "request.",
        TurnKeyArgs,
        _hints(False, destructive=True, open_world=True),
        _TURN_OUT,
        retry_task,
    ),
    ToolSpec(
        "sbx_acknowledge_unknown",
        "Acknowledge a Turn whose outcome could not be proven, so new work can follow. Does not "
        "retry or cancel anything.",
        TurnArgs,
        _hints(False),
        _TURN_OUT,
        acknowledge_unknown,
    ),
)

BY_NAME = {spec.name: spec for spec in TOOLS}
