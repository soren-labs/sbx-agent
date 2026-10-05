"""Session application: create/accept/send/cancel/retry/archive/close (RFC 02).

Every command is one transaction: authorize, dedupe, lock in defined order,
verify lifecycle/version, mutate typed projections, append journal facts and
enqueue deduped Jobs. Nothing here performs network or process calls.
"""

from __future__ import annotations

from typing import Any

from control.application import access
from control.application.ports import HarnessCatalog, SessionResolver, Transactions
from control.application.projections import session_resource, turn_resource
from control.domain import sessions as rules
from control.domain.digests import digest_of
from control.domain.errors import DomainError
from control.domain.identity import Principal
from control.domain.ids import new_id
from control.jobs.claims import cancel_active


def _content(body: dict[str, Any]) -> list[dict[str, Any]]:
    content = body.get("content")
    if isinstance(content, str):
        content = [{"kind": "text", "text": content}]
    if not isinstance(content, list) or not content:
        raise DomainError("validation_failed", "message content is required")
    out = []
    for part in content:
        if not isinstance(part, dict) or part.get("kind") not in (
            "text",
            "file_ref",
            "changeset_ref",
        ):
            raise DomainError("validation_failed", "unsupported content part")
        if part["kind"] == "text" and not str(part.get("text", "")).strip():
            raise DomainError("validation_failed", "text part is empty")
        out.append(dict(part))
    return out


def insert_message(
    uow: Any,
    session: dict[str, Any],
    *,
    content: list[dict[str, Any]],
    routing: str,
    author_kind: str,
    author_id: str | None,
    role: str = "user",
    source_message_id: str | None = None,
    source_session_id: str | None = None,
    reply_to_message_id: str | None = None,
    actor: str,
) -> dict[str, Any]:
    ordinal = uow.allocate(session["id"], "next_message_ordinal")
    message = uow.insert(
        "messages",
        {
            "id": new_id("message"),
            "workspace_id": session["workspace_id"],
            "session_id": session["id"],
            "ordinal": ordinal,
            "author_kind": author_kind,
            "author_id": author_id,
            "role": role,
            "routing": routing,
            "content": content,
            "content_digest": digest_of(content),
            "source_message_id": source_message_id,
            "source_session_id": source_session_id,
            "reply_to_message_id": reply_to_message_id,
        },
    )
    uow.append_event(
        session,
        "message.accepted",
        {
            "message_id": message["id"],
            "ordinal": ordinal,
            "author_kind": author_kind,
            "author_id": author_id,
            "role": role,
            "content_digest": message["content_digest"],
            "source_session_id": source_session_id,
        },
        actor=actor,
    )
    return message


def queue_turn(
    uow: Any,
    session: dict[str, Any],
    message: dict[str, Any],
    *,
    actor: str,
    settings: dict[str, Any] | None = None,
    result_contract: dict[str, Any] | None = None,
    retry_of_turn_id: str | None = None,
) -> dict[str, Any]:
    ordinal = uow.allocate(session["id"], "next_turn_ordinal")
    effective = {
        "provider_id": session["harness_provider"],
        "model": session["harness_model"],
        **(session["effective_spec"].get("turn_defaults") or {}),
        **(settings or {}),
    }
    turn = uow.insert(
        "turns",
        {
            "id": new_id("turn"),
            "workspace_id": session["workspace_id"],
            "session_id": session["id"],
            "ordinal": ordinal,
            "triggering_message_id": message["id"],
            "settings": effective,
            "result_contract": result_contract,
            "retry_of_turn_id": retry_of_turn_id,
        },
    )
    uow.update("messages", message["id"], {"turn_id": turn["id"]})
    uow.append_event(
        session,
        "message.routed",
        {"message_id": message["id"], "requested": message["routing"], "effective": "queue"},
        actor=actor,
        turn_id=turn["id"],
    )
    uow.append_event(
        session,
        "turn.queued",
        {
            "ordinal": ordinal,
            "message_id": message["id"],
            "settings": effective,
            "retry_of_turn_id": retry_of_turn_id,
        },
        actor=actor,
        turn_id=turn["id"],
    )
    schedule_next(uow, session)
    return turn


def schedule_next(uow: Any, session: dict[str, Any]) -> str | None:
    """Enqueue dispatch for the head queued Turn when no Turn is active."""
    if session["lifecycle"] != "open":
        return None
    if uow.count("turns", {"session_id": session["id"], "state": list(rules.ACTIVE_TURN_STATES)}):
        return None
    if uow.count(
        "turns",
        {
            "session_id": session["id"],
            "state": "interrupted",
            "unknown_acknowledged_at": None,
            "reason": "outcome_unknown",
        },
    ):
        # Unknown outcomes require explicit acknowledgement before new work (RFC 02).
        return None
    head = uow.query_one("turns.next_queued", session_id=session["id"])
    if head is None:
        return None
    return uow.enqueue_job(
        workspace_id=session["workspace_id"],
        kind="turn.dispatch",
        target_id=head["id"],
        session_id=session["id"],
    )


def finish_turn(
    uow: Any,
    session: dict[str, Any],
    turn: dict[str, Any],
    state: str,
    *,
    actor: str,
    reason: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    evidence_complete: bool | None = None,
    outcome: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    execution_id: str | None = None,
) -> dict[str, Any]:
    """The single terminalization path: exactly one terminal verdict per Turn."""
    rules.TURN.check(turn["state"], state)
    now = uow.now()
    updated = uow.update(
        "turns",
        turn["id"],
        {
            "state": state,
            "reason": reason,
            "error_code": error_code,
            "error_message": (error_message or None) and error_message[:2000],
            "evidence_complete": evidence_complete,
            "outcome": outcome,
            "usage": usage,
            "finished_at": now,
        },
        expect={"state": turn["state"], "version": turn["version"]},
        bump_version=True,
    )
    if updated is None:
        raise DomainError("version_conflict", "turn changed concurrently")
    uow.update("sessions", session["id"], {"active_turn_id": None, "updated_at": now})
    uow.append_event(
        session,
        f"turn.{state}",
        {
            "reason": reason,
            "error_code": error_code,
            "evidence_complete": evidence_complete,
            "outcome": outcome or {},
        },
        actor=actor,
        turn_id=turn["id"],
        execution_id=execution_id,
    )
    schedule_next(uow, {**session, "active_turn_id": None})
    return updated


class Sessions:
    def __init__(
        self, tx: Transactions, resolver: SessionResolver, catalog: HarnessCatalog
    ) -> None:
        self.tx = tx
        self.resolver = resolver
        self.catalog = catalog
        # Close side effects owned by other applications (e.g. Delegation subtree cancel).
        self.close_hooks: list[Any] = []

    # -- creation -----------------------------------------------------------------
    def create(
        self,
        principal: Principal,
        workspace_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        parent: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        access.require_workspace(principal, workspace_id)

        def fn(uow: Any) -> dict[str, Any]:
            replay = uow.dedupe_begin(
                principal_id=principal.user_id,
                workspace_id=workspace_id,
                command_kind="sessions.create",
                key=idempotency_key,
                request=body,
            )
            if replay is not None:
                return replay
            response = self.create_in(uow, principal, workspace_id, body)
            uow.dedupe_finish(
                principal_id=principal.user_id,
                workspace_id=workspace_id,
                command_kind="sessions.create",
                key=idempotency_key,
                response=response,
            )
            return response

        return self.tx.run(fn)

    def create_in(
        self,
        uow: Any,
        principal: Principal,
        workspace_id: str,
        body: dict[str, Any],
        *,
        parent_session_id: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        actor = actor or principal.user_id
        spec = self.resolver.resolve(uow, principal, workspace_id, body)
        role = body.get("role") or "developer"
        rules.check_role(role)
        session_id = new_id("session")
        session = uow.insert(
            "sessions",
            {
                "id": session_id,
                "workspace_id": workspace_id,
                "role": role,
                "title": (body.get("title") or "")[:200],
                "labels": [str(x)[:40] for x in (body.get("labels") or [])][:20],
                "created_by": principal.user_id,
                "project_id": spec.get("project_id"),
                "project_version_id": spec.get("project_version_id"),
                "harness_provider": spec["harness"]["provider_id"],
                "harness_model": spec["harness"].get("model"),
                "executor_backend": spec["executor"]["backend"],
                "resource_class": spec["executor"].get("resource_class", "standard"),
                "compute_connection_id": spec["connections"].get("compute"),
                "inference_connection_id": spec["connections"].get("inference"),
                "source_connection_id": spec["connections"].get("source"),
                "effective_spec": spec,
                "effective_spec_digest": digest_of(spec),
                "parent_session_id": parent_session_id,
                "linked_from_session_id": body.get("linked_from_session_id"),
            },
        )
        repo = spec.get("repository") or {}
        uow.insert(
            "worktrees",
            {
                "id": new_id("worktree"),
                "workspace_id": workspace_id,
                "session_id": session_id,
                "repository": repo.get("full_name"),
                "base_ref": repo.get("base_ref"),
                "base_sha": repo.get("base_sha"),
            },
        )
        uow.append_event(
            session,
            "session.created",
            {
                "role": role,
                "effective_spec_digest": session["effective_spec_digest"],
                "project_version_id": spec.get("project_version_id"),
                "harness": spec["harness"],
                "executor": spec["executor"],
                "parent_session_id": parent_session_id,
            },
            actor=actor,
        )
        response: dict[str, Any] = {"session_id": session_id}
        if body.get("message"):
            message_body = body["message"]
            result = self._accept(uow, principal, session, message_body, actor=actor)
            response.update(result)
        session = uow.get("sessions", session_id)
        response["session"] = session_resource(uow, session)
        response["event_watermark"] = uow.watermark(session_id)
        return response

    # -- messages -------------------------------------------------------------------
    def send(
        self,
        principal: Principal,
        session_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            session = access.owned(
                uow, principal, "sessions", session_id, lock=True, what="session"
            )
            replay = uow.dedupe_begin(
                principal_id=principal.user_id,
                workspace_id=session["workspace_id"],
                command_kind=f"messages.create:{session_id}",
                key=idempotency_key,
                request=body,
            )
            if replay is not None:
                return replay
            response = self._accept(uow, principal, session, body, actor=principal.user_id)
            response["event_watermark"] = uow.watermark(session_id)
            uow.dedupe_finish(
                principal_id=principal.user_id,
                workspace_id=session["workspace_id"],
                command_kind=f"messages.create:{session_id}",
                key=idempotency_key,
                response=response,
            )
            return response

        return self.tx.run(fn)

    def _accept(
        self,
        uow: Any,
        principal: Principal | None,
        session: dict[str, Any],
        body: dict[str, Any],
        *,
        actor: str,
        author_kind: str = "user",
        source_session_id: str | None = None,
        result_contract: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        routing = body.get("routing") or "queue"
        rules.check_routing(routing)
        content = _content(body)
        if session["lifecycle"] == "closed":
            rules.check_accepts_work("closed")
        if routing != "note":
            rules.check_accepts_work(session["lifecycle"])
        effective = routing
        if routing == "steer":
            supported = self.catalog.capability(session["harness_provider"], "steer") == "supported"
            active = session.get("active_turn_id")
            if not supported or not active:
                if body.get("fallback") != "queue":
                    raise DomainError(
                        "unsupported_capability",
                        "steer is not supported by the installed Harness; "
                        "pass fallback=queue to queue explicitly",
                        details={"capability": "steer", "provider_id": session["harness_provider"]},
                    )
                effective = "queue"
            else:  # pragma: no cover - no verified steer Harness is enabled yet
                raise DomainError("unsupported_capability", "steer injection not enabled")
        settings = body.get("settings") or None
        if settings:
            self._check_settings(session, settings)
        message = insert_message(
            uow,
            session,
            content=content,
            routing=routing,
            author_kind=author_kind,
            author_id=principal.user_id if principal else None,
            source_session_id=source_session_id,
            reply_to_message_id=body.get("reply_to_message_id"),
            actor=actor,
        )
        response: dict[str, Any] = {"message_id": message["id"], "routing": effective}
        if effective == "note":
            uow.append_event(
                session,
                "message.routed",
                {"message_id": message["id"], "requested": "note", "effective": "note"},
                actor=actor,
            )
            return response
        turn = queue_turn(
            uow,
            session,
            message,
            actor=actor,
            settings=settings,
            result_contract=result_contract or body.get("result_contract"),
        )
        response["turn_id"] = turn["id"]
        response["turn"] = turn_resource(turn)
        return response

    def _check_settings(self, session: dict[str, Any], settings: dict[str, Any]) -> None:
        allowed = {"model", "effort"}
        unknown = set(settings) - allowed
        if unknown:
            raise DomainError("validation_failed", f"unknown settings {sorted(unknown)}")
        if (
            "effort" in settings
            and self.catalog.capability(session["harness_provider"], "effort_settings")
            != "supported"
        ):
            raise DomainError(
                "unsupported_capability",
                "effort settings are not supported by this Harness",
                details={"capability": "effort_settings"},
            )

    # -- cancellation / retry --------------------------------------------------------
    def cancel_turn(
        self, principal: Principal, turn_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            turn = access.owned(uow, principal, "turns", turn_id, what="turn")
            session = uow.get("sessions", turn["session_id"], lock=True)
            turn = uow.get("turns", turn_id, lock=True)
            return self.cancel_in(uow, session, turn, actor=principal.user_id)

        return self.tx.run(fn)

    def cancel_in(
        self,
        uow: Any,
        session: dict[str, Any],
        turn: dict[str, Any],
        *,
        actor: str,
        reason: str = "cancelled_by_user",
    ) -> dict[str, Any]:
        state = turn["state"]
        if rules.TURN.is_terminal(state) or state == "cancelling":
            # A committed verdict wins; repeated intent is idempotent.
            return {"turn": turn_resource(turn), "event_watermark": uow.watermark(session["id"])}
        now = uow.now()
        uow.append_event(
            session,
            "turn.cancel_requested",
            {"actor": actor, "reason": reason},
            actor=actor,
            turn_id=turn["id"],
        )
        if state == "queued":
            cancel_active(uow, kind="turn.dispatch", dedupe_key=turn["id"], reason=reason)
            uow.update("turns", turn["id"], {"cancel_requested_at": now, "cancel_actor": actor})
            turn = uow.get("turns", turn["id"])
            turn = finish_turn(
                uow,
                session,
                turn,
                "cancelled",
                actor=actor,
                reason=reason,
                evidence_complete=True,
                outcome={"started": False},
            )
        else:
            turn = uow.update(
                "turns",
                turn["id"],
                {
                    "state": "cancelling",
                    "cancel_requested_at": now,
                    "cancel_actor": actor,
                    "reason": reason,
                },
                expect={"state": state},
                bump_version=True,
            )
            execution = uow.find_one(
                "executions",
                {"turn_id": turn["id"], "state": ["preparing", "started", "stop_requested"]},
            )
            if execution is not None:
                uow.enqueue_job(
                    workspace_id=session["workspace_id"],
                    kind="execution.reconcile",
                    target_id=execution["id"],
                    dedupe_key=f"{execution['id']}:cancel",
                    input={"intent": "cancel"},
                    priority=80,
                    session_id=session["id"],
                )
            else:
                # Preparing without a launched attempt: dispatch observes intent and seals.
                uow.enqueue_job(
                    workspace_id=session["workspace_id"],
                    kind="turn.dispatch",
                    target_id=turn["id"],
                    session_id=session["id"],
                    priority=80,
                )
        return {"turn": turn_resource(turn), "event_watermark": uow.watermark(session["id"])}

    def retry_turn(
        self, principal: Principal, turn_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            turn = access.owned(uow, principal, "turns", turn_id, what="turn")
            session = uow.get("sessions", turn["session_id"], lock=True)
            replay = uow.dedupe_begin(
                principal_id=principal.user_id,
                workspace_id=session["workspace_id"],
                command_kind=f"turns.retry:{turn_id}",
                key=idempotency_key,
                request={},
            )
            if replay is not None:
                return replay
            if turn["state"] not in ("failed", "cancelled", "interrupted"):
                raise DomainError(
                    "invalid_transition",
                    "only failed, cancelled or interrupted Turns can be retried",
                )
            if turn["reason"] == "outcome_unknown" and turn["unknown_acknowledged_at"] is None:
                raise DomainError(
                    "outcome_unknown",
                    "acknowledge the unknown outcome before retrying",
                    action="acknowledge",
                )
            rules.check_accepts_work(session["lifecycle"])
            original = uow.get("messages", turn["triggering_message_id"])
            message = insert_message(
                uow,
                session,
                content=original["content"],
                routing="queue",
                author_kind="user",
                author_id=principal.user_id,
                source_message_id=original["id"],
                actor=principal.user_id,
            )
            new_turn = queue_turn(
                uow,
                session,
                message,
                actor=principal.user_id,
                settings=turn["settings"],
                result_contract=turn["result_contract"],
                retry_of_turn_id=turn["id"],
            )
            response = {
                "message_id": message["id"],
                "turn_id": new_turn["id"],
                "turn": turn_resource(new_turn),
                "event_watermark": uow.watermark(session["id"]),
            }
            uow.dedupe_finish(
                principal_id=principal.user_id,
                workspace_id=session["workspace_id"],
                command_kind=f"turns.retry:{turn_id}",
                key=idempotency_key,
                response=response,
            )
            return response

        return self.tx.run(fn)

    def acknowledge_unknown(self, principal: Principal, turn_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            turn = access.owned(uow, principal, "turns", turn_id, what="turn")
            session = uow.get("sessions", turn["session_id"], lock=True)
            if turn["reason"] != "outcome_unknown":
                raise DomainError("invalid_transition", "turn outcome is not unknown")
            quarantined = uow.count(
                "executor_leases",
                {
                    "session_id": session["id"],
                    "quarantined": True,
                    "state": ["allocating", "ready", "quiescing"],
                },
            )
            if quarantined:
                raise DomainError(
                    "outcome_unknown", "old compute is not yet confirmed isolated", retryable=True
                )
            turn = uow.update("turns", turn_id, {"unknown_acknowledged_at": uow.now()})
            schedule_next(uow, session)
            return {"turn": turn_resource(turn), "event_watermark": uow.watermark(session["id"])}

        return self.tx.run(fn)

    # -- lifecycle --------------------------------------------------------------------
    def update(self, principal: Principal, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            session = access.owned(
                uow, principal, "sessions", session_id, lock=True, what="session"
            )
            expected = body.get("expected_version")
            if expected is None:
                raise DomainError("validation_failed", "expected_version is required")
            if int(expected) != session["version"]:
                raise DomainError(
                    "version_conflict",
                    "session version changed",
                    details={"current_version": session["version"]},
                )
            changes: dict[str, Any] = {}
            if "title" in body:
                changes["title"] = str(body["title"] or "")[:200]
            if "labels" in body:
                changes["labels"] = [str(x)[:40] for x in body["labels"] or []][:20]
            if not changes:
                raise DomainError("validation_failed", "nothing to update")
            session = uow.update(
                "sessions", session_id, {**changes, "updated_at": uow.now()}, bump_version=True
            )
            uow.append_event(session, "session.settings_changed", changes, actor=principal.user_id)
            return {
                "session": session_resource(uow, session),
                "event_watermark": uow.watermark(session_id),
            }

        return self.tx.run(fn)

    def set_lifecycle(
        self,
        principal: Principal,
        session_id: str,
        target: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            session = access.owned(
                uow, principal, "sessions", session_id, lock=True, what="session"
            )
            return self.lifecycle_in(uow, session, target, actor=principal.user_id)

        return self.tx.run(fn)

    def lifecycle_in(
        self, uow: Any, session: dict[str, Any], target: str, *, actor: str
    ) -> dict[str, Any]:
        if session["lifecycle"] == target:
            return {
                "session": session_resource(uow, session),
                "event_watermark": uow.watermark(session["id"]),
            }
        rules.SESSION.check(session["lifecycle"], target)
        now = uow.now()
        values: dict[str, Any] = {"lifecycle": target, "updated_at": now}
        if target == "archived":
            values["archived_at"] = now
        if target == "closed":
            values["closed_at"] = now
        previous = session["lifecycle"]
        session = uow.update("sessions", session["id"], values, bump_version=True)
        event = {
            "open": "session.unarchived",
            "archived": "session.archived",
            "closed": "session.closed",
        }[target]
        uow.append_event(session, event, {"from": previous}, actor=actor)
        if target == "open":
            schedule_next(uow, session)
        if target == "closed":
            for turn in uow.find(
                "turns",
                {"session_id": session["id"], "state": ["queued", "preparing", "running"]},
                order="ordinal",
                lock=True,
            ):
                self.cancel_in(uow, session, turn, actor=actor, reason="session_closed")
            lease = uow.find_one(
                "executor_leases",
                {"session_id": session["id"], "state": ["allocating", "ready", "quiescing"]},
            )
            if lease is not None:
                uow.enqueue_job(
                    workspace_id=session["workspace_id"],
                    kind="executor.release",
                    target_id=lease["id"],
                    session_id=session["id"],
                    input={"reason": "session_closed"},
                )
            for hook in self.close_hooks:
                hook(uow, session, actor)
        return {
            "session": session_resource(uow, uow.get("sessions", session["id"])),
            "event_watermark": uow.watermark(session["id"]),
        }
