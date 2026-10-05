"""Generic Delegation — review/test/research/integration child Sessions
(RFC 167 §05).

There is no review engine: every Delegation spawns an ordinary child Session
with its own Worktree and a typed ResultContract. The contract's pinned
subject digest, verdict vocabulary and evidence requirements are validated
at publication — assistant text alone never satisfies the gate.
"""

from __future__ import annotations

import base64
import hashlib
import json

from protocol.runtime import OperationEnvelope, OperationKind

from control.domain import ids
from control.domain.delegation import (
    DELEGATION_TERMINAL,
    VERDICTS,
    DelegationState,
    WaitState,
    require_delegation_transition,
)
from control.domain.errors import DomainError, NotFound
from control.domain.jobs import JobKind, TargetFamily
from control.domain.sessions import TurnState
from control.persistence.unit_of_work import SqlUnitOfWork

from .events import append_event
from .sessions import enqueue_job

#: Result file the child writes inside its own Worktree. The platform reads
#: it through the runtime — never from assistant prose.
RESULT_PATH = ".sbx/result.json"

CONTRACT_KINDS = frozenset(
    {
        "ReviewAssessment",
        "TestResult",
        "ResearchResult",
        "IntegrationResult",
        "GenericResult",
    }
)

DEFAULT_BUDGET = {
    "max_children": 8,
    "max_depth": 4,
    "deadline_seconds": 3600,
}


def _result_schema_check(contract: dict, value: dict) -> list[str]:
    """Gate-critical fields are typed; extensions are versioned."""
    errs: list[str] = []
    kind = contract.get("kind") or "GenericResult"
    for pin in contract.get("subject_pins") or []:
        if pin.get("digest") and value.get("subject_digest") != pin["digest"]:
            errs.append(
                f"subject_digest mismatch: expected {pin['digest']},"
                f" got {value.get('subject_digest')}"
            )
    if kind == "ReviewAssessment":
        if value.get("verdict") not in VERDICTS:
            errs.append(f"verdict missing/invalid: {value.get('verdict')!r}")
        if not isinstance(value.get("findings", []), list):
            errs.append("findings must be a list")
    if kind == "TestResult":
        checks = value.get("checks")
        if not isinstance(checks, list) or not checks:
            errs.append("checks must be a non-empty list")
        elif not all(isinstance(c, dict) and c.get("name") for c in checks):
            errs.append("each check requires a name")
    return errs


class DelegationService:
    def __init__(self, db, *, runtime_stack=None) -> None:
        self.db = db
        self.stack = runtime_stack

    # ------------------------------------------------------------- spawn
    def spawn(
        self,
        uow: SqlUnitOfWork,
        *,
        principal: dict,
        workspace_id: str,
        parent_session_id: str,
        role: str,
        result_contract: dict,
        inputs: list[dict],
        prompt: str,
        harness: dict,
        budget: dict | None = None,
        project_version_id: str | None = None,
        projectless_spec: dict | None = None,
        dedupe_key: str | None = None,
    ) -> dict:
        """Atomically create child Session + Worktree + Delegation + initial
        Message/Turn + dispatch Job. Inputs reference immutable subjects —
        never a live parent path."""
        parent = uow.sessions.get_for_update(workspace_id, parent_session_id)
        if parent is None:
            raise NotFound("parent session")
        kind = (result_contract or {}).get("kind") or "GenericResult"
        if kind not in CONTRACT_KINDS:
            raise DomainError("validation_failed", f"unknown result kind {kind!r}")
        budget = {**DEFAULT_BUDGET, **(budget or {})}
        # Depth: walk the delegation chain up; cap.
        depth = 1
        node = parent
        while depth <= int(budget["max_depth"]) + 1:
            up = uow.delegations.get_by_child_session(workspace_id, node["id"])
            if up is None:
                break
            depth += 1
            node = uow.sessions.get(workspace_id, up["parent_session_id"]) or parent
        if depth > int(budget["max_depth"]):
            raise DomainError(
                "rate_limited", "delegation depth cap exceeded", details={"depth": depth}
            )
        children = uow.delegations.children_of(workspace_id, parent_session_id)
        active_children = [c for c in children if c["state"] not in tuple(DELEGATION_TERMINAL)]
        if len(active_children) >= int(budget["max_children"]):
            raise DomainError("rate_limited", "delegation children cap exceeded")
        for inp in inputs:
            if inp.get("kind") == "changeset":
                cs = uow.changesets.get(workspace_id, inp["ref"])
                if cs is None:
                    raise NotFound("changeset")
                if inp.get("digest") and inp["digest"] != cs["subject_digest"]:
                    raise DomainError(
                        "version_conflict",
                        "input digest does not match the pinned subject",
                    )

        child_id = ids.new_id("session")
        worktree_id = ids.new_id("worktree")
        effective_input = {
            "delegation": True,
            "role": role,
            "result_contract": result_contract,
            "inputs": inputs,
            "parent_session_id": parent_session_id,
        }
        input_digest = (
            "sha256:"
            + hashlib.sha256(json.dumps(effective_input, sort_keys=True).encode()).hexdigest()
        )
        uow.sessions.insert(
            {
                "id": child_id,
                "workspace_id": workspace_id,
                "role": role,
                "title": f"{role} of {parent_session_id}",
                "created_by": principal.get("id"),
                "project_version_id": project_version_id,
                "projectless_spec": projectless_spec,
                "harness": harness,
                "effective_input": effective_input,
                "effective_input_digest": input_digest,
                "linked_from_session_id": parent_session_id,
            }
        )
        uow.worktrees.insert(
            {
                "id": worktree_id,
                "workspace_id": workspace_id,
                "session_id": child_id,
                "availability": "none",
            }
        )
        delegation_id = ids.new_id("delegation")
        uow.delegations.insert(
            {
                "id": delegation_id,
                "workspace_id": workspace_id,
                "parent_session_id": parent_session_id,
                "child_session_id": child_id,
                "role": role,
                "state": DelegationState.ACTIVE.value,
                "result_contract": result_contract,
                "input_refs": {"items": [i.get("ref") for i in inputs]},
                "input_digest": input_digest,
                "budget": budget,
            }
        )
        for inp in inputs:
            uow.delegation_inputs.insert(
                {
                    "id": ids.new_id("delegation_input"),
                    "workspace_id": workspace_id,
                    "delegation_id": delegation_id,
                    "kind": inp["kind"],
                    "ref": inp["ref"],
                    "digest": inp.get("digest"),
                }
            )
        append_event(
            uow,
            workspace_id=workspace_id,
            session_id=parent_session_id,
            event_type="delegation.spawned",
            payload={
                "delegation_id": delegation_id,
                "child_session_id": child_id,
                "role": role,
                "input_digest": input_digest,
            },
        )
        append_event(
            uow,
            workspace_id=workspace_id,
            session_id=child_id,
            event_type="session.created",
            payload={
                "role": role,
                "delegation_id": delegation_id,
                "parent_session_id": parent_session_id,
                "worktree_id": worktree_id,
            },
        )
        first = self._enqueue_turn(
            uow,
            workspace_id=workspace_id,
            session_id=child_id,
            author={"kind": "delegation", "id": delegation_id},
            content={
                "text": prompt,
                "delegation_id": delegation_id,
                "role": role,
                "result_contract": result_contract,
                "inputs": inputs,
            },
            role="user",
        )
        return {
            "delegation_id": delegation_id,
            "child_session_id": child_id,
            "worktree_id": worktree_id,
            **first,
        }

    # ------------------------------------------------------------- message
    def _enqueue_turn(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        session_id: str,
        author: dict,
        content: dict,
        role: str = "user",
    ) -> dict:
        """Queue a message + Turn + dispatch Job into a session — the
        durable wake primitive shared by spawn, send and waiter wakeup."""
        msg_ordinal = uow.sessions.allocate(workspace_id, session_id, "next_message_ordinal")
        message_id = ids.new_id("message")
        uow.messages.insert(
            {
                "id": message_id,
                "workspace_id": workspace_id,
                "session_id": session_id,
                "ordinal": msg_ordinal,
                "author": author,
                "role": role,
                "routing": "queue",
                "content": content,
                "attachment_refs": [],
                "routed_routing": "queue",
            }
        )
        append_event(
            uow,
            workspace_id=workspace_id,
            session_id=session_id,
            event_type="message.accepted",
            payload={"message_id": message_id, "ordinal": msg_ordinal},
        )
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
        uow.conn.execute("UPDATE messages SET routed_turn_id=%s WHERE id=%s", (turn_id, message_id))
        append_event(
            uow,
            workspace_id=workspace_id,
            session_id=session_id,
            event_type="turn.queued",
            turn_id=turn_id,
            payload={"turn_id": turn_id, "ordinal": turn_ordinal, "message_id": message_id},
        )
        enqueue_job(
            uow,
            workspace_id=workspace_id,
            kind=JobKind.TURN_DISPATCH,
            target_family=TargetFamily.TURN,
            target_id=turn_id,
            dedupe_key=f"turn.dispatch:{turn_id}",
            payload={"turn_id": turn_id, "session_id": session_id},
        )
        return {"message_id": message_id, "turn_id": turn_id}

    def send(
        self,
        uow: SqlUnitOfWork,
        *,
        principal: dict,
        workspace_id: str,
        delegation_id: str,
        content: dict,
    ) -> dict:
        d = uow.delegations.get(workspace_id, delegation_id)
        if d is None:
            raise NotFound("delegation")
        if d["state"] in tuple(DELEGATION_TERMINAL):
            raise DomainError("invalid_state", f"delegation is {d['state']}")
        return self._enqueue_turn(
            uow,
            workspace_id=workspace_id,
            session_id=d["child_session_id"],
            author={"kind": "session", "id": d["parent_session_id"]},
            content=content,
        )

    # ------------------------------------------------------------- wait
    def wait(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        subscriber_session_id: str,
        delegation_id: str,
        predicate: dict | None = None,
        deadline_seconds: int = 1800,
    ) -> dict:
        """Persist a wait subscription; the satisfaction check happens in
        the same transaction so an already-published result can never be
        missed."""
        d = uow.delegations.get_for_update(workspace_id, delegation_id)
        if d is None:
            raise NotFound("delegation")
        wait_id = ids.new_id("wait_subscription")
        existing = uow.delegation_results.get_for(workspace_id, delegation_id)
        if existing is not None or d["state"] in tuple(DELEGATION_TERMINAL):
            uow.wait_subscriptions.insert(
                {
                    "id": wait_id,
                    "workspace_id": workspace_id,
                    "delegation_id": delegation_id,
                    "subscriber_session_id": subscriber_session_id,
                    "predicate": predicate or {"result": "terminal"},
                    "state": WaitState.SATISFIED.value,
                    "satisfied_by_result_id": existing["id"] if existing else None,
                    "result_version": (existing or {}).get("version", 1),
                }
            )
            self._wake_subscriber(
                uow,
                workspace_id=workspace_id,
                subscriber_session_id=subscriber_session_id,
                delegation=d,
                result=existing,
            )
            return {"wait_id": wait_id, "state": "satisfied"}
        uow.wait_subscriptions.insert(
            {
                "id": wait_id,
                "workspace_id": workspace_id,
                "delegation_id": delegation_id,
                "subscriber_session_id": subscriber_session_id,
                "predicate": predicate or {"result": "terminal"},
                "state": WaitState.PENDING.value,
            }
        )
        uow.conn.execute(
            "UPDATE wait_subscriptions SET deadline_at = now() +"
            " (%s || ' seconds')::interval WHERE id=%s",
            (deadline_seconds, wait_id),
        )
        return {"wait_id": wait_id, "state": "pending"}

    # ------------------------------------------------------------- cancel
    def cancel(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        delegation_id: str,
        execution_service=None,
    ) -> dict:
        d = uow.delegations.get_for_update(workspace_id, delegation_id)
        if d is None:
            raise NotFound("delegation")
        require_delegation_transition(DelegationState(d["state"]), DelegationState.CANCELLED)
        active_turn = uow.turns.active_by_session(workspace_id, d["child_session_id"])
        if active_turn is not None:
            uow.turns.update(
                workspace_id,
                active_turn["id"],
                {"state": TurnState.CANCELLED.value, "reason": "delegation_cancelled"},
            )
        uow.wait_subscriptions.cancel_for_delegation(workspace_id, delegation_id)
        uow.wait_subscriptions.cancel_for_session(workspace_id, d["child_session_id"])
        uow.delegations.update(
            workspace_id, delegation_id, {"state": DelegationState.CANCELLED.value}
        )
        append_event(
            uow,
            workspace_id=workspace_id,
            session_id=d["parent_session_id"],
            event_type="delegation.cancelled",
            payload={"delegation_id": delegation_id},
        )
        return {"delegation_id": delegation_id, "state": "cancelled"}

    # ------------------------------------------------------------- result
    def result(self, uow: SqlUnitOfWork, *, workspace_id: str, delegation_id: str) -> dict | None:
        return uow.delegation_results.get_for(workspace_id, delegation_id)

    # ------------------------------------------------------- finalize (job)
    def publish_result(self, job: dict, ctx) -> dict:
        """Validate the child's typed result and publish one immutable
        DelegationResult; satisfy pending waiters with a durable wake."""
        workspace_id = job["workspace_id"]
        delegation_id = (job.get("payload") or {}).get("delegation_id") or job["target_id"]
        with SqlUnitOfWork(self.db, actor={"kind": "job", "id": job["id"]}) as uow:
            d = uow.delegations.get_for_update(workspace_id, delegation_id)
            if d is None or d["state"] in tuple(DELEGATION_TERMINAL):
                uow.commit()
                return {"skipped": "delegation terminal or gone"}
            child_turn = uow.rows.one(
                "SELECT * FROM turns WHERE workspace_id=%s AND session_id=%s"
                " ORDER BY ordinal DESC LIMIT 1",
                (workspace_id, d["child_session_id"]),
            )
            if child_turn is None or child_turn["state"] not in (
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
            ):
                uow.commit()
                from control.jobs.worker import JobRetry

                raise JobRetry("child turn not yet terminal")
            if child_turn["state"] != "succeeded":
                # A failed/cancelled child publishes a failed Delegation —
                # never an approval.
                uow.delegations.update(
                    workspace_id,
                    delegation_id,
                    {"state": DelegationState.FAILED.value},
                )
                self._wake_waiters(uow, workspace_id=workspace_id, delegation=d, result=None)
                append_event(
                    uow,
                    workspace_id=workspace_id,
                    session_id=d["parent_session_id"],
                    event_type="delegation.failed",
                    payload={
                        "delegation_id": delegation_id,
                        "reason": f"child turn {child_turn['state']}",
                    },
                )
                uow.commit()
                return {"state": "failed", "reason": child_turn["state"]}
            uow.delegations.update(
                workspace_id,
                delegation_id,
                {"state": DelegationState.WAITING_RESULT.value},
            )
            uow.commit()
        value, evidence_refs, errs = self._read_result(workspace_id, d)
        validation = "invalid" if errs else "valid"
        errs = errs + _result_schema_check(d["result_contract"] or {}, value or {})
        if errs:
            validation = "invalid"
        with SqlUnitOfWork(self.db, actor={"kind": "job", "id": job["id"]}) as uow:
            d = uow.delegations.get_for_update(workspace_id, delegation_id)
            if d["state"] in tuple(DELEGATION_TERMINAL):
                uow.commit()
                return {"skipped": "delegation terminal"}
            result_id = ids.new_id("delegation_result")
            uow.delegation_results.insert(
                {
                    "id": result_id,
                    "workspace_id": workspace_id,
                    "delegation_id": delegation_id,
                    "completing_turn_id": child_turn["id"],
                    "child_session_id": d["child_session_id"],
                    "contract_version": int(
                        (d["result_contract"] or {}).get("schema_version") or 1
                    ),
                    "subject_digest": (value or {}).get("subject_digest"),
                    "head_sha": (value or {}).get("head_sha"),
                    "verdict": (value or {}).get("verdict"),
                    "validation_status": validation,
                    "value": value or {},
                    "evidence_refs": evidence_refs,
                }
            )
            new_state = (
                DelegationState.SUCCEEDED.value
                if validation == "valid"
                else DelegationState.FAILED.value
            )
            uow.delegations.update(workspace_id, delegation_id, {"state": new_state})
            append_event(
                uow,
                workspace_id=workspace_id,
                session_id=d["parent_session_id"],
                event_type=f"delegation.{new_state}",
                payload={
                    "delegation_id": delegation_id,
                    "result_id": result_id,
                    "validation_status": validation,
                    "errors": errs,
                    "verdict": (value or {}).get("verdict"),
                },
            )
            result_row = uow.delegation_results.get_for(workspace_id, delegation_id)
            self._wake_waiters(uow, workspace_id=workspace_id, delegation=d, result=result_row)
            uow.commit()
        return {
            "delegation_id": delegation_id,
            "state": new_state,
            "validation": validation,
            "errors": errs,
        }

    def _read_result(self, workspace_id: str, delegation: dict) -> tuple:
        """Read ``.sbx/result.json`` from the child's Worktree through its
        live lease. The file's bytes are the evidence — its digest is
        recorded, never its provenance re-trusted."""
        if self.stack is None:
            return None, [], ["runtime plane not wired"]
        with SqlUnitOfWork(self.db, actor={"kind": "finalize"}) as uow:
            session = uow.sessions.get(workspace_id, delegation["child_session_id"])
            lease = uow.leases.active_by_session(workspace_id, session["id"])
            uow.commit()
        if lease is None or not self.stack.pool.alive(lease["id"]):
            return None, [], ["child worktree unreadable — no live lease"]
        env = OperationEnvelope(
            operation_id=ids.new_id("effect"),
            operation_kind=OperationKind.FILES_READ,
            session_id=session["id"],
            lease_id=lease["id"],
            lease_generation=lease["generation"],
            payload={"path": RESULT_PATH, "root": "worktree"},
        )
        try:
            reply = self.stack.pool.submit_for_result(lease["id"], env.to_dict(), timeout=60.0)
        except Exception as exc:
            return None, [], [f"result read failed: {type(exc).__name__}"]
        if reply.get("state") != "succeeded":
            return None, [], ["no result file in child worktree"]
        data = base64.b64decode((reply.get("result") or {}).get("content_b64") or "")
        try:
            value = json.loads(data)
        except Exception:
            return None, [], ["result file is not valid JSON"]
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        return value, [{"kind": "result_file", "digest": digest, "path": RESULT_PATH}], []

    def _wake_waiters(self, uow, *, workspace_id, delegation, result) -> None:
        subs = uow.wait_subscriptions.pending_for(workspace_id, delegation["id"])
        for sub in subs:
            uow.wait_subscriptions.satisfy(
                workspace_id,
                sub["id"],
                result_id=(result or {}).get("id"),
                result_version=(result or {}).get("version", 1),
            )
            self._wake_subscriber(
                uow,
                workspace_id=workspace_id,
                subscriber_session_id=sub["subscriber_session_id"],
                delegation=delegation,
                result=result,
            )

    def _wake_subscriber(
        self, uow, *, workspace_id, subscriber_session_id, delegation, result
    ) -> None:
        """Durable wake: queue a follow-up Message + Turn on the waiting
        session — survives worker restarts because it's all in this tx."""
        if result is None and delegation["state"] in (
            DelegationState.FAILED.value,
            DelegationState.CANCELLED.value,
        ):
            content = {
                "kind": "delegation.result",
                "delegation_id": delegation["id"],
                "state": delegation["state"],
                "result": None,
            }
        else:
            content = {
                "kind": "delegation.result",
                "delegation_id": delegation["id"],
                "result": {
                    "id": (result or {}).get("id"),
                    "verdict": (result or {}).get("verdict"),
                    "subject_digest": (result or {}).get("subject_digest"),
                    "value": (result or {}).get("value") or {},
                    "validation_status": (result or {}).get("validation_status"),
                },
            }
        self._enqueue_turn(
            uow,
            workspace_id=workspace_id,
            session_id=subscriber_session_id,
            author={"kind": "delegation", "id": delegation["id"]},
            content=content,
            role="system",
        )
