"""Pure resource queries. Reads never allocate, dispatch, settle or validate (RFC 04 A10)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from control.application import access
from control.domain import sessions as rules
from control.domain.errors import DomainError
from control.domain.identity import Principal


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def turn_resource(turn: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": turn["id"],
        "session_id": turn["session_id"],
        "ordinal": turn["ordinal"],
        "state": turn["state"],
        "reason": turn["reason"],
        "triggering_message_id": turn["triggering_message_id"],
        "retry_of_turn_id": turn["retry_of_turn_id"],
        "settings": turn["settings"],
        "result_contract": turn["result_contract"],
        "error": {"code": turn["error_code"], "message": turn["error_message"]}
        if turn["error_code"]
        else None,
        "evidence_complete": turn["evidence_complete"],
        "outcome": turn["outcome"],
        "usage": turn["usage"],
        "output_message_id": turn["output_message_id"],
        "cancel_requested": turn["cancel_requested_at"] is not None,
        "unknown_acknowledged": turn["unknown_acknowledged_at"] is not None,
        "version": turn["version"],
        "created_at": _iso(turn["created_at"]),
        "started_at": _iso(turn["started_at"]),
        "finished_at": _iso(turn["finished_at"]),
        "actions": _turn_actions(turn),
    }


def _turn_actions(turn: dict[str, Any]) -> list[str]:
    state = turn["state"]
    actions = []
    if state in ("queued", "preparing", "running"):
        actions.append("cancel")
    if state in ("failed", "cancelled", "interrupted"):
        if turn["reason"] == "outcome_unknown" and turn["unknown_acknowledged_at"] is None:
            actions.append("acknowledge_unknown")
        else:
            actions.append("retry")
    return actions


def session_resource(
    uow: Any, session: dict[str, Any], activity: dict[str, Any] | None = None
) -> dict[str, Any]:
    if activity is None:
        rows = uow.query("turns.activity", session_ids=[session["id"]])
        activity = rows[0] if rows else {"active": None, "queued": 0, "last_terminal": None}
    worktree = uow.find_one("worktrees", {"session_id": session["id"]})
    lease = (
        uow.get("executor_leases", session["active_lease_id"])
        if session["active_lease_id"]
        else None
    )
    state = rules.activity_of(
        session["lifecycle"], activity["active"], activity["queued"], activity["last_terminal"]
    )
    actions = []
    if session["lifecycle"] == "open":
        actions += ["send", "archive", "close"]
    elif session["lifecycle"] == "archived":
        actions += ["unarchive", "close", "note"]
    return {
        "id": session["id"],
        "workspace_id": session["workspace_id"],
        "lifecycle": session["lifecycle"],
        "activity": state,
        "role": session["role"],
        "title": session["title"],
        "labels": list(session["labels"] or []),
        "project_id": session["project_id"],
        "project_version_id": session["project_version_id"],
        "harness": {"provider_id": session["harness_provider"], "model": session["harness_model"]},
        "executor": {
            "backend": session["executor_backend"],
            "resource_class": session["resource_class"],
            "lease_id": lease["id"] if lease else None,
            "lease_state": lease["state"] if lease else None,
        },
        "connections": {
            "compute": session["compute_connection_id"],
            "inference": session["inference_connection_id"],
            "source": session["source_connection_id"],
        },
        "worktree": {
            "id": worktree["id"],
            "availability": worktree["availability"],
            "generation": worktree["generation"],
            "repository": worktree["repository"],
            "base_sha": worktree["base_sha"],
            "recovery_point": worktree["recovery_point"],
        }
        if worktree
        else None,
        "parent_session_id": session["parent_session_id"],
        "linked_from_session_id": session["linked_from_session_id"],
        "active_turn_id": session["active_turn_id"],
        "effective_spec_digest": session["effective_spec_digest"],
        "version": session["version"],
        "created_at": _iso(session["created_at"]),
        "updated_at": _iso(session["updated_at"]),
        "actions": actions,
    }


def message_resource(message: dict[str, Any], parts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": message["id"],
        "session_id": message["session_id"],
        "ordinal": message["ordinal"],
        "role": message["role"],
        "author_kind": message["author_kind"],
        "routing": message["routing"],
        "content": message["content"],
        "turn_id": message["turn_id"],
        "state": message["state"],
        "source_session_id": message["source_session_id"],
        "parts": [
            {
                "id": p["id"],
                "key": p["part_key"],
                "kind": p["kind"],
                "ordinal": p["ordinal"],
                "revision": p["revision"],
                "content": p["content"],
                "data": p["data"],
                "sealed": p["sealed"],
            }
            for p in parts
        ],
        "created_at": _iso(message["created_at"]),
    }


class Queries:
    def __init__(self, tx: Any) -> None:
        self.tx = tx

    def session(self, principal: Principal, session_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            session = access.owned(uow, principal, "sessions", session_id, what="session")
            return {
                "session": session_resource(uow, session),
                "event_watermark": uow.watermark(session_id),
            }

        return self.tx.read(fn)

    def sessions(
        self,
        principal: Principal,
        workspace_id: str,
        *,
        lifecycle: str | None = None,
        role: str | None = None,
        project_id: str | None = None,
        parent_session_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        access.require_workspace(principal, workspace_id)
        limit = max(1, min(int(limit), 200))
        cursor_at = cursor_id = None
        if cursor:
            try:
                at, cursor_id = cursor.split("|", 1)
                cursor_at = datetime.fromisoformat(at)
            except ValueError as exc:
                raise DomainError("invalid_cursor", "malformed cursor") from exc

        def fn(uow: Any) -> dict[str, Any]:
            rows = uow.query(
                "sessions.page",
                workspace_ids=[workspace_id],
                lifecycle=lifecycle,
                role=role,
                project_id=project_id,
                parent_session_id=parent_session_id,
                cursor_at=cursor_at,
                cursor_id=cursor_id,
                limit=limit + 1,
            )
            page = rows[:limit]
            stats = (
                {
                    r["session_id"]: r
                    for r in uow.query("turns.activity", session_ids=[r["id"] for r in page])
                }
                if page
                else {}
            )
            items = [
                session_resource(
                    uow, s, stats.get(s["id"], {"active": None, "queued": 0, "last_terminal": None})
                )
                for s in page
            ]
            next_cursor = (
                f"{page[-1]['updated_at'].isoformat()}|{page[-1]['id']}"
                if len(rows) > limit
                else None
            )
            return {"items": items, "next_cursor": next_cursor}

        return self.tx.read(fn)

    def messages(self, principal: Principal, session_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            access.owned(uow, principal, "sessions", session_id, what="session")
            messages = uow.find("messages", {"session_id": session_id}, order="ordinal")
            parts: dict[str, list[dict[str, Any]]] = {}
            for part in uow.query("parts.for_session", session_id=session_id):
                parts.setdefault(part["message_id"], []).append(part)
            return {
                "items": [message_resource(m, parts.get(m["id"], [])) for m in messages],
                "event_watermark": uow.watermark(session_id),
            }

        return self.tx.read(fn)

    def turns(self, principal: Principal, session_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            access.owned(uow, principal, "sessions", session_id, what="session")
            turns = uow.find("turns", {"session_id": session_id}, order="ordinal")
            return {
                "items": [turn_resource(t) for t in turns],
                "event_watermark": uow.watermark(session_id),
            }

        return self.tx.read(fn)

    def turn(self, principal: Principal, turn_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            turn = access.owned(uow, principal, "turns", turn_id, what="turn")
            executions = uow.find("executions", {"turn_id": turn_id}, order="attempt_ordinal")
            body = turn_resource(turn)
            body["executions"] = [
                {
                    "id": e["id"],
                    "attempt_ordinal": e["attempt_ordinal"],
                    "state": e["state"],
                    "lease_id": e["executor_lease_id"],
                    "launch_evidence": e["launch_evidence"],
                    "cli_version": e["cli_version"],
                    "adapter_version": e["adapter_version"],
                    "image_digest": e["image_digest"],
                    "final_watermark": e["final_watermark"],
                    "outcome": e["outcome"],
                }
                for e in executions
            ]
            return {"turn": body, "event_watermark": uow.watermark(turn["session_id"])}

        return self.tx.read(fn)

    def events(
        self,
        principal: Principal,
        session_id: str,
        *,
        after: int = 0,
        limit: int = 500,
        types: list[str] | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 1000))
        if after < 0:
            raise DomainError("invalid_cursor", "after must be >= 0")

        def fn(uow: Any) -> dict[str, Any]:
            access.owned(uow, principal, "sessions", session_id, what="session")
            watermark = uow.watermark(session_id)
            if after > watermark:
                raise DomainError(
                    "invalid_cursor",
                    "cursor is ahead of the committed journal",
                    details={"event_watermark": watermark},
                )
            scanned = uow.query_one(
                "events.max_scanned",
                session_id=session_id,
                after=after,
                upto=watermark,
                limit=limit,
            )["seq"]
            rows = uow.query(
                "events.after",
                session_id=session_id,
                after=after,
                upto=scanned,
                types=types,
                turn_id=turn_id,
                limit=limit,
            )
            return {
                "items": [event_envelope(r) for r in rows],
                "next_after": int(scanned),
                "event_watermark": watermark,
            }

        return self.tx.read(fn)


def event_envelope(row: dict[str, Any]) -> dict[str, Any]:
    from protocol.events import ENVELOPE_FIELDS

    out = {k: _iso(row.get(k)) for k in ENVELOPE_FIELDS}
    return out
