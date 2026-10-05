"""Session commands — create, message/turn intake, lifecycle (RFC 167 §04).

Message routing: `note` records only; `queue` creates a queued Turn plus a
deduped turn.dispatch Job; `steer` attaches to the active Turn. The Session
row is the serialization point: every command locks it FOR UPDATE first.
"""

from __future__ import annotations

from control.domain import ids
from control.domain.errors import DomainError, NotFound
from control.domain.jobs import JobKind, TargetFamily
from control.domain.sessions import (
    MessageRouting,
    SessionLifecycle,
    TurnState,
)
from control.persistence.base import _now
from control.persistence.unit_of_work import SqlUnitOfWork

from .commands import CommandBus, canonical_request_digest
from .events import append_event


def _err(code: str, msg: str, **kw) -> DomainError:
    return DomainError(code, msg, **kw)


def enqueue_job(
    uow: SqlUnitOfWork,
    *,
    workspace_id: str,
    kind: JobKind | str,
    target_family: TargetFamily | str,
    target_id: str | None,
    dedupe_key: str,
    payload: dict | None = None,
    priority: int = 100,
    effect_id: str | None = None,
) -> dict:
    """Insert a durable Job with active-state dedupe. A duplicate intent
    (same dedupe_key) returns the existing Job — effects stay once-only."""
    kind_s = kind.value if isinstance(kind, JobKind) else kind
    fam_s = target_family.value if isinstance(target_family, TargetFamily) else target_family
    existing = uow.jobs.find_active_by_dedupe(dedupe_key)
    if existing:
        return existing
    return uow.jobs.insert(
        {
            "id": ids.new_id("job"),
            "workspace_id": workspace_id,
            "kind": kind_s,
            "target_family": fam_s,
            "target_id": target_id,
            "effect_id": effect_id or ids.new_id("effect"),
            "dedupe_key": dedupe_key,
            "payload": payload or {},
            "priority": priority,
        }
    )


def enqueue_outbox(
    uow: SqlUnitOfWork,
    *,
    workspace_id: str,
    destination: str,
    kind: str,
    dedupe_key: str,
    payload: dict,
    subscriber: str | None = None,
    event_id: str | None = None,
) -> dict:
    """Transactional outbox record — dedupe_key makes re-emit a no-op."""
    existing = uow.outbox.get_by_dedupe(dedupe_key)
    if existing:
        return existing
    return uow.outbox.insert(
        {
            "id": ids.new_id("outbox_message"),
            "workspace_id": workspace_id,
            "destination": destination,
            "subscriber": subscriber,
            "event_id": event_id,
            "kind": kind,
            "dedupe_key": dedupe_key,
            "payload": payload,
        }
    )


class SessionService:
    """Session-scoped commands. Constructed over a Database + CommandBus."""

    def __init__(self, db) -> None:
        self.bus = CommandBus(db)

    # ---------------------------------------------------------------
    def create_session(
        self,
        *,
        principal: dict,
        workspace_id: str,
        role: str = "author",
        title: str | None = None,
        project_version_id: str | None = None,
        projectless_spec: dict | None = None,
        harness: dict,
        effective_input: dict | None = None,
        effective_input_digest: str,
        linked_from_session_id: str | None = None,
        dedupe_key: str | None = None,
        labels: list | None = None,
    ) -> dict:
        """Create a Session + its Worktree + the session.created event."""
        body = {
            "workspace_id": workspace_id,
            "role": role,
            "title": title,
            "project_version_id": project_version_id,
            "projectless_spec": projectless_spec,
            "harness": harness,
            "effective_input": effective_input,
            "effective_input_digest": effective_input_digest,
            "linked_from_session_id": linked_from_session_id,
            "labels": labels or [],
        }
        digest = canonical_request_digest(body)

        def handler(uow: SqlUnitOfWork) -> dict:
            if project_version_id:
                pv = uow.project_versions.get(workspace_id, project_version_id)
                if pv is None:
                    raise NotFound("project_version")
            session_id = ids.new_id("session")
            uow.sessions.insert(
                {
                    "id": session_id,
                    "workspace_id": workspace_id,
                    "role": role,
                    "title": title,
                    "labels": labels or [],
                    "created_by": principal.get("id"),
                    "project_version_id": project_version_id,
                    "projectless_spec": projectless_spec,
                    "harness": harness,
                    "effective_input": effective_input or {},
                    "effective_input_digest": effective_input_digest,
                    "linked_from_session_id": linked_from_session_id,
                }
            )
            # Every Session owns exactly one logical Worktree from birth.
            worktree_id = ids.new_id("worktree")
            uow.worktrees.insert(
                {
                    "id": worktree_id,
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "availability": "none",
                }
            )
            ev = append_event(
                uow,
                workspace_id=workspace_id,
                session_id=session_id,
                event_type="session.created",
                payload={
                    "role": role,
                    "project_version_id": project_version_id,
                    "harness": harness,
                    "worktree_id": worktree_id,
                },
            )
            return {"session_id": session_id, "worktree_id": worktree_id, "event_seq": ev["seq"]}

        return self.bus.run(
            principal=principal,
            workspace_id=workspace_id,
            command_kind="session.create",
            dedupe_key=dedupe_key,
            request_digest=digest,
            handler=handler,
        )

    # ---------------------------------------------------------------
    def post_message(
        self,
        *,
        principal: dict,
        workspace_id: str,
        session_id: str,
        author: dict,
        routing: str,
        content: dict,
        role: str = "user",
        attachment_refs: list | None = None,
        reply_to_message_id: str | None = None,
        source_message_id: str | None = None,
        dedupe_key: str | None = None,
    ) -> dict:
        """Accept input under the Session lock; routing decides the effect."""
        body = {
            "session_id": session_id,
            "author": author,
            "routing": routing,
            "content": content,
            "role": role,
            "attachment_refs": attachment_refs or [],
            "reply_to_message_id": reply_to_message_id,
            "source_message_id": source_message_id,
        }
        digest = canonical_request_digest(body)

        def handler(uow: SqlUnitOfWork) -> dict:
            session = uow.sessions.get_for_update(workspace_id, session_id)
            if session is None:
                raise NotFound("session")
            if session["lifecycle"] != SessionLifecycle.OPEN.value:
                raise _err(
                    "invalid_state",
                    f"session is {session['lifecycle']} — not accepting work",
                )
            try:
                route = MessageRouting(routing)
            except ValueError:
                raise _err("validation_failed", f"bad routing {routing!r}")

            active = uow.turns.active_by_session(workspace_id, session_id)
            if route == MessageRouting.STEER and active is None:
                raise _err(
                    "invalid_state",
                    "steer requires an active turn — use queue instead",
                )

            ordinal = uow.sessions.allocate(workspace_id, session_id, "next_message_ordinal")
            message_id = ids.new_id("message")
            uow.messages.insert(
                {
                    "id": message_id,
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "ordinal": ordinal,
                    "author": author,
                    "role": role,
                    "routing": route.value,
                    "content": content,
                    "attachment_refs": attachment_refs or [],
                    "reply_to_message_id": reply_to_message_id,
                    "source_message_id": source_message_id,
                    "routed_routing": route.value,
                }
            )
            ev = append_event(
                uow,
                workspace_id=workspace_id,
                session_id=session_id,
                event_type="message.accepted",
                turn_id=active["id"] if active else None,
                payload={
                    "message_id": message_id,
                    "ordinal": ordinal,
                    "routing": route.value,
                },
            )
            out = {
                "message_id": message_id,
                "ordinal": ordinal,
                "event_seq": ev["seq"],
                "routing": route.value,
            }

            if route == MessageRouting.QUEUE:
                turn_ordinal = uow.sessions.allocate(workspace_id, session_id, "next_turn_ordinal")
                turn_id = ids.new_id("turn")
                uow.turns.insert(
                    {
                        "id": turn_id,
                        "workspace_id": workspace_id,
                        "session_id": session_id,
                        "ordinal": turn_ordinal,
                        "message_id": message_id,
                        "state": TurnState.QUEUED.value,
                    }
                )
                uow.conn.execute(
                    "UPDATE messages SET routed_turn_id=%s WHERE id=%s",
                    (turn_id, message_id),
                )
                tev = append_event(
                    uow,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    event_type="turn.queued",
                    turn_id=turn_id,
                    causation_id=ev["id"],
                    payload={
                        "turn_id": turn_id,
                        "ordinal": turn_ordinal,
                        "message_id": message_id,
                    },
                )
                job = enqueue_job(
                    uow,
                    workspace_id=workspace_id,
                    kind=JobKind.TURN_DISPATCH,
                    target_family=TargetFamily.TURN,
                    target_id=turn_id,
                    dedupe_key=f"turn.dispatch:{turn_id}",
                    payload={"turn_id": turn_id, "session_id": session_id},
                )
                out.update(
                    {
                        "turn_id": turn_id,
                        "turn_ordinal": turn_ordinal,
                        "turn_event_seq": tev["seq"],
                        "dispatch_job": job["id"],
                    }
                )
            elif route == MessageRouting.STEER:
                sev = append_event(
                    uow,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    event_type="message.steer_attached",
                    turn_id=active["id"],
                    causation_id=ev["id"],
                    payload={
                        "message_id": message_id,
                        "turn_id": active["id"],
                    },
                )
                uow.conn.execute(
                    "INSERT INTO turn_messages (turn_id, message_id, link_kind)"
                    " VALUES (%s, %s, 'steer_ack') ON CONFLICT DO NOTHING",
                    (active["id"], message_id),
                )
                out.update({"turn_id": active["id"], "steer_event_seq": sev["seq"]})
            return out

        return self.bus.run(
            principal=principal,
            workspace_id=workspace_id,
            command_kind="session.post_message",
            dedupe_key=dedupe_key,
            request_digest=digest,
            handler=handler,
        )

    # ---------------------------------------------------------------
    def close_session(
        self,
        *,
        principal: dict,
        workspace_id: str,
        session_id: str,
        dedupe_key: str | None = None,
    ) -> dict:
        """Lifecycle close: cancel pending waits, release claimable state."""
        digest = canonical_request_digest({"session_id": session_id, "op": "close"})

        def handler(uow: SqlUnitOfWork) -> dict:
            session = uow.sessions.get_for_update(workspace_id, session_id)
            if session is None:
                raise NotFound("session")
            if session["lifecycle"] == SessionLifecycle.CLOSED.value:
                return {"session_id": session_id, "already": True}
            uow.wait_subscriptions.cancel_for_session(workspace_id, session_id)
            uow.sessions.update(
                workspace_id,
                session_id,
                {"lifecycle": SessionLifecycle.CLOSED.value, "closed_at": _now()},
            )
            ev = append_event(
                uow,
                workspace_id=workspace_id,
                session_id=session_id,
                event_type="session.closed",
                payload={},
            )
            return {"session_id": session_id, "event_seq": ev["seq"]}

        return self.bus.run(
            principal=principal,
            workspace_id=workspace_id,
            command_kind="session.close",
            dedupe_key=dedupe_key,
            request_digest=digest,
            handler=handler,
        )
