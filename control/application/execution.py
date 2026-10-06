"""Execution application: admission, capacity, lease allocation/adoption, Worktree
realization, fenced start, reconciliation, runtime loss and release (RFC 03).

Decisions commit in transactions under the current Job claim; executor and
runtime calls happen between transactions with stable operation IDs.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from protocol.runtime import PROTOCOL_MAJOR, compatible

from control.application import access
from control.application.artifact_secrets import runtime_visible
from control.application.ingest import IngestHooks, ingest
from control.application.ports import (
    BlobStore,
    CredentialBroker,
    ExecutorBackend,
    HarnessCatalog,
    RuntimeConnector,
    RuntimeRefused,
    RuntimeUnavailable,
    Transactions,
)
from control.application.sessions import finish_turn, unacknowledged_unknown
from control.domain.digests import sha256_hex
from control.domain.errors import DomainError
from control.domain.execution import LIVE_EXECUTION_STATES, LIVE_LEASE_STATES
from control.domain.identity import Principal
from control.domain.ids import new_id
from control.domain.sessions import ACTIVE_TURN_STATES
from control.jobs.model import Continue, Outcome, Retry, Succeeded

_TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")
_FAIL_REASONS = (
    "executor_unavailable",
    "runtime_incompatible",
    "credential_invalid",
    "connection_required",
    "connection_revoked",
    "unsupported_capability",
)


@dataclass
class ExecutionSettings:
    turn_deadline_seconds: float = 3600.0
    poll_busy: float = 0.3
    poll_idle: float = 1.0
    unreachable_threshold: int = 5
    capacity_retry: float = 2.0
    # Upper bound for an in-flight backend create; after it, "no allocation found for
    # the operation" is authoritative even if an allocate was requested.
    allocation_window_seconds: float = 600.0


def compose_prompt(message: dict[str, Any], turn: dict[str, Any]) -> str:
    texts = [str(p.get("text", "")) for p in message["content"] if p.get("kind") == "text"]
    refs = [p for p in message["content"] if p.get("kind") != "text"]
    prompt = "\n\n".join(texts)
    if refs:
        lines = [f"- {r.get('kind')}: {r.get('ref') or r.get('id')}" for r in refs]
        prompt += "\n\nReferenced inputs:\n" + "\n".join(lines)
    contract = turn["result_contract"]
    if contract and contract.get("instructions"):
        prompt += "\n\n" + str(contract["instructions"])
    return prompt


class ExecutionService:
    def __init__(
        self,
        tx: Transactions,
        *,
        executors: dict[str, ExecutorBackend],
        connector: RuntimeConnector,
        credentials: CredentialBroker,
        blobs: BlobStore,
        catalog: HarnessCatalog,
        settings: ExecutionSettings | None = None,
    ) -> None:
        self.tx = tx
        self.executors = executors
        self.connector = connector
        self.credentials = credentials
        self.blobs = blobs
        self.catalog = catalog
        self.settings = settings or ExecutionSettings()
        self.hooks = IngestHooks(credential_health=self._credential_health)
        # Called after a Worktree is realized on a lease (e.g. Delegation input ChangeSets).
        self.post_restore_hooks: list[Any] = []

    # ======================================================================== commands
    def activate(self, principal: Principal, session_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            session = access.owned(
                uow, principal, "sessions", session_id, lock=True, what="session"
            )
            if session["lifecycle"] == "closed":
                raise DomainError("invalid_transition", "closed sessions cannot be activated")
            lease = self._live_lease(uow, session) or self._new_lease(uow, session)
            job = uow.enqueue_job(
                workspace_id=session["workspace_id"],
                kind="executor.allocate",
                target_id=lease["id"],
                session_id=session_id,
            )
            return {
                "lease_id": lease["id"],
                "job_id": job,
                "event_watermark": uow.watermark(session_id),
            }

        return self.tx.run(fn)

    def release(self, principal: Principal, session_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            session = access.owned(
                uow, principal, "sessions", session_id, lock=True, what="session"
            )
            lease = self._live_lease(uow, session)
            if lease is None:
                raise DomainError("executor_unavailable", "no live executor lease")
            job = uow.enqueue_job(
                workspace_id=session["workspace_id"],
                kind="executor.release",
                target_id=lease["id"],
                session_id=session_id,
                input={"reason": "user"},
            )
            return {
                "lease_id": lease["id"],
                "job_id": job,
                "event_watermark": uow.watermark(session_id),
            }

        return self.tx.run(fn)

    def executor_view(self, principal: Principal, session_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            session = access.owned(uow, principal, "sessions", session_id, what="session")
            leases = uow.find(
                "executor_leases", {"session_id": session_id}, order="generation DESC", limit=5
            )
            worktree = uow.find_one("worktrees", {"session_id": session_id})
            snap = (
                uow.get("snapshots", worktree["last_snapshot_id"])
                if worktree["last_snapshot_id"]
                else None
            )
            return {
                "backend": session["executor_backend"],
                "leases": [
                    {
                        "id": lease["id"],
                        "generation": lease["generation"],
                        "state": lease["state"],
                        "reason": lease["state_reason"],
                        "quarantined": lease["quarantined"],
                        "image_digest": lease["image_digest"],
                        "protocol_version": lease["protocol_version"],
                        "created_at": lease["created_at"].isoformat(),
                    }
                    for lease in leases
                ],
                "worktree": {
                    "id": worktree["id"],
                    "availability": worktree["availability"],
                    "generation": worktree["generation"],
                    "base_sha": worktree["base_sha"],
                },
                "recovery_point": {
                    "snapshot_id": snap["id"],
                    "generation": snap["worktree_generation"],
                    "created_at": snap["created_at"].isoformat(),
                }
                if snap
                else None,
                "event_watermark": uow.watermark(session_id),
            }

        return self.tx.read(fn)

    # ========================================================================= helpers
    def _live_lease(self, uow: Any, session: dict[str, Any]) -> dict[str, Any] | None:
        return uow.find_one(
            "executor_leases",
            {"session_id": session["id"], "state": list(LIVE_LEASE_STATES)},
            lock=True,
        )

    def _new_lease(self, uow: Any, session: dict[str, Any]) -> dict[str, Any]:
        if uow.count("executor_leases", {"session_id": session["id"], "quarantined": True}):
            raise DomainError(
                "outcome_unknown",
                "previous compute is quarantined until isolation is confirmed",
                retryable=True,
            )
        generation = uow.count("executor_leases", {"session_id": session["id"]}) + 1
        lease = uow.insert(
            "executor_leases",
            {
                "id": new_id("lease"),
                "workspace_id": session["workspace_id"],
                "session_id": session["id"],
                "backend": session["executor_backend"],
                "allocation_operation_id": new_id("operation"),
                "generation": generation,
                "compute_connection_id": session["compute_connection_id"],
                "image_digest": "pending",
                "resource_class": session["resource_class"],
            },
        )
        uow.update("sessions", session["id"], {"active_lease_id": lease["id"]})
        return lease

    def _backend(self, lease: dict[str, Any]) -> ExecutorBackend:
        backend = self.executors.get(lease["backend"])
        if backend is None:
            raise DomainError(
                "executor_unavailable", f"executor backend {lease['backend']} is not configured"
            )
        return backend

    def _compute(self, session: dict[str, Any]) -> dict[str, Any] | None:
        return self.credentials.compute(session) if session["executor_backend"] != "local" else None

    def _credential_health(
        self, uow: Any, session: dict[str, Any], execution: dict[str, Any], health: str
    ) -> None:
        self.credentials.report_health(
            uow, execution["inference_connection_id"], execution["credential_version_id"], health
        )

    # ========================================================================= dispatch
    def handle_dispatch(self, ctx: Any) -> Outcome:
        turn_id = ctx.claim.job["turn_id"]
        plan = ctx.commit(lambda uow: self._admit(uow, turn_id))
        if plan["action"] == "done":
            return Succeeded(plan.get("result"))
        if plan["action"] == "wait":
            return Continue(delay=plan.get("delay", self.settings.capacity_retry))
        last_attempt = ctx.claim.attempts >= ctx.claim.job["max_attempts"] - 1
        try:
            lease = self._ensure_lease(ctx, plan["lease_id"])
            if lease is None:
                return Continue(delay=1.0)
            self._ensure_worktree(ctx, lease)
            created = ctx.commit(lambda uow: self._create_execution(uow, turn_id, lease["id"]))
            if created is None:
                return Succeeded({"skipped": True})
            if created == "wait":
                return Continue(delay=self.settings.capacity_retry)
            self._start(ctx, created)
            return Succeeded({"execution_id": created})
        except DomainError as exc:
            if exc.retryable and not last_attempt:
                return Retry(exc.code, exc.message, retry_after=exc.retry_after)
            ctx.commit(lambda uow, e=exc: self._fail_preparing(uow, turn_id, e.code, e.message))
            return Succeeded({"failed": exc.code})
        except RuntimeUnavailable as exc:
            if not last_attempt:
                return Retry("executor_unavailable", str(exc)[:200])
            ctx.commit(
                lambda uow: self._fail_preparing(
                    uow, turn_id, "executor_unavailable", "runtime unreachable"
                )
            )
            return Succeeded({"failed": "executor_unavailable"})

    def _admit(self, uow: Any, turn_id: str) -> dict[str, Any]:
        turn = uow.get("turns", turn_id)
        session = uow.get("sessions", turn["session_id"], lock=True)
        turn = uow.get("turns", turn_id, lock=True)
        live = uow.find_one(
            "executions", {"turn_id": turn_id, "state": list(LIVE_EXECUTION_STATES)}
        )
        if turn["state"] == "cancelling":
            if live is None:
                finish_turn(
                    uow,
                    session,
                    turn,
                    "cancelled",
                    actor="application",
                    reason="cancelled_by_user",
                    evidence_complete=True,
                    outcome={"started": False},
                )
            return {"action": "done", "result": {"cancelled": True}}
        if turn["state"] not in ("queued", "preparing"):
            return {"action": "done"}
        if session["lifecycle"] != "open" and turn["state"] == "queued":
            return {"action": "done", "result": {"paused": session["lifecycle"]}}
        other = uow.find_one(
            "turns", {"session_id": session["id"], "state": list(ACTIVE_TURN_STATES)}
        )
        if other is not None and other["id"] != turn_id:
            return {"action": "done", "result": {"waiting_for": other["id"]}}
        if (
            live is None
            and turn["state"] == "queued"
            and unacknowledged_unknown(uow, session["id"])
        ):
            # A stale/duplicate dispatch Job must not bypass the acknowledgement gate;
            # acknowledge_unknown re-schedules the head Turn.
            return {"action": "done", "result": {"blocked": "outcome_unknown"}}
        if live is not None:
            uow.enqueue_job(
                workspace_id=session["workspace_id"],
                kind="execution.reconcile",
                target_id=live["id"],
                session_id=session["id"],
            )
            return {"action": "done", "result": {"adopted": live["id"]}}
        if turn["state"] == "queued":
            turn = uow.update(
                "turns",
                turn_id,
                {"state": "preparing", "reason": "waiting_executor"},
                expect={"state": "queued"},
                bump_version=True,
            )
            uow.update(
                "sessions", session["id"], {"active_turn_id": turn_id, "updated_at": uow.now()}
            )
            uow.append_event(
                session,
                "turn.preparing",
                {"reason": "waiting_executor"},
                actor="application",
                turn_id=turn_id,
            )
        try:
            self.credentials.check_inference(uow, session)
            if session["executor_backend"] == "modal" and not session["compute_connection_id"]:
                raise DomainError(
                    "connection_required",
                    "a Modal compute Connection is required",
                    details={"kind": "modal"},
                )
        except DomainError as exc:
            reason = exc.code if exc.code in _FAIL_REASONS else "credential_invalid"
            finish_turn(
                uow,
                session,
                turn,
                "failed",
                actor="application",
                reason=reason,
                error_code=exc.code,
                error_message=exc.message,
                evidence_complete=True,
                outcome={"started": False},
            )
            return {"action": "done", "result": {"failed": exc.code}}
        lease = self._live_lease(uow, session)
        if lease is None:
            if uow.count("executor_leases", {"session_id": session["id"], "quarantined": True}):
                return {"action": "wait", "delay": 5.0}
            lease = self._new_lease(uow, session)
        return {"action": "run", "lease_id": lease["id"]}

    def _fail_preparing(self, uow: Any, turn_id: str, code: str, message: str) -> None:
        turn = uow.get("turns", turn_id)
        session = uow.get("sessions", turn["session_id"], lock=True)
        turn = uow.get("turns", turn_id, lock=True)
        if turn["state"] in ("queued", "preparing"):
            if turn["state"] == "queued":
                turn = uow.update("turns", turn_id, {"state": "preparing"}, bump_version=True)
            reason = code if code in _FAIL_REASONS else "executor_unavailable"
            finish_turn(
                uow,
                session,
                turn,
                "failed",
                actor="application",
                reason=reason,
                error_code=code,
                error_message=message,
                evidence_complete=True,
                outcome={"started": False},
            )
        elif turn["state"] == "cancelling":
            finish_turn(
                uow,
                session,
                turn,
                "cancelled",
                actor="application",
                reason="cancelled_by_user",
                evidence_complete=True,
            )

    # ----------------------------------------------------------------------- lease
    def _ensure_lease(self, ctx: Any, lease_id: str) -> dict[str, Any] | None:
        lease = ctx.db.read(lambda uow: uow.get("executor_leases", lease_id))
        if lease["state"] == "ready":
            return lease
        if lease["state"] != "allocating":
            return None
        session = ctx.db.read(lambda uow: uow.get("sessions", lease["session_id"]))
        backend = self._backend(lease)
        compute = self._compute(session)
        spec = {
            "workspace_id": lease["workspace_id"],
            "session_id": lease["session_id"],
            "lease_id": lease["id"],
            "generation": lease["generation"],
            "allocation_operation_id": lease["allocation_operation_id"],
            "resource_class": lease["resource_class"],
            "network_policy": "egress",
            "protocol_majors": [PROTOCOL_MAJOR],
            "enrollment_key": self.connector.enrollment_key(lease),
            "compute": compute,
            "compute_connection_id": lease["compute_connection_id"],
        }
        operation_id = lease["allocation_operation_id"]
        try:
            if lease["handle"]:
                # Identity already persisted: observe it; never allocate a twin under it.
                status = backend.describe(lease["handle"], compute).get("status")
                found = {**lease["handle"], "status": status}
            else:
                if not ctx.commit(lambda uow: self._mark_allocating(uow, lease["id"])):
                    return None
                # Idempotent by operation identity: adopt before allocating.
                found = backend.lookup(operation_id, compute) or backend.allocate(
                    spec, operation_id
                )
            if found.get("status") == "terminated":
                ctx.commit(
                    lambda uow: self._lose_lease(
                        uow, lease["id"], "allocation_terminated", confirmed=True
                    )
                )
                raise DomainError(
                    "executor_unavailable",
                    "the allocation ended before binding; a new lease will be allocated",
                    retryable=True,
                )
            handle = {k: v for k, v in found.items() if k != "status"}
            if not lease["handle"]:
                # Persist the handle before the runtime handshake so a crash or a
                # concurrent release can always find and terminate this allocation.
                if not ctx.commit(lambda uow: self._record_handle(uow, lease["id"], handle)):
                    backend.terminate(handle, f"{lease['id']}:terminate", compute)
                    return None
            ctx.renew()
            endpoint = backend.connect_runtime(handle, compute)
        except DomainError as exc:
            if exc.code in ("credential_invalid", "connection_revoked", "unsupported_capability"):
                ctx.commit(lambda uow, e=exc: self._lose_unbound(uow, lease["id"], e.code))
            raise
        bound = {**lease, "handle": {**handle, "endpoint": endpoint}}
        hello = self.connector.channel(bound).hello()
        if not compatible(int(hello["protocol"]["major"])) or hello["lease_id"] != lease["id"]:
            backend.terminate(handle, f"{lease['id']}:terminate", compute)
            ctx.commit(
                lambda uow: self._lose_lease(
                    uow, lease["id"], "runtime_incompatible", confirmed=True
                )
            )
            raise DomainError(
                "runtime_incompatible",
                "runtime protocol or lease identity mismatch",
                retryable=False,
            )

        def bind(uow: Any) -> dict[str, Any]:
            current = uow.get("executor_leases", lease["id"], lock=True)
            if current["state"] != "allocating":
                return current
            row = uow.update(
                "executor_leases",
                lease["id"],
                {
                    "state": "ready",
                    "handle": bound["handle"],
                    "image_digest": hello["image_digest"],
                    "runtime_epoch": hello["runtime_epoch"],
                    "protocol_version": f"{hello['protocol']['major']}.{hello['protocol']['minor']}",
                    "capabilities": {"harnesses": hello["harnesses"]},
                    "compute_credential_version_id": (compute or {}).get("credential_version_id"),
                    "bound_at": uow.now(),
                    "observed_status": "ready",
                    "observed_at": uow.now(),
                },
            )
            session_row = uow.get("sessions", lease["session_id"], lock=True)
            uow.append_event(
                session_row,
                "executor.bound",
                {
                    "lease_id": lease["id"],
                    "generation": lease["generation"],
                    "backend": lease["backend"],
                    "image_digest": hello["image_digest"],
                    "protocol_version": row["protocol_version"],
                    "runtime_epoch": hello["runtime_epoch"],
                },
                actor="application",
                executor_lease_id=lease["id"],
                lease_generation=lease["generation"],
            )
            return row

        return ctx.commit(bind)

    def _mark_allocating(self, uow: Any, lease_id: str) -> bool:
        """Durable intent before any backend create; refused once a release fenced it."""
        lease = uow.get("executor_leases", lease_id, lock=True)
        if lease["state"] != "allocating":
            return False
        uow.update(
            "executor_leases",
            lease_id,
            {"observed_status": "allocate_requested", "observed_at": uow.now()},
        )
        return True

    def _record_handle(self, uow: Any, lease_id: str, handle: dict[str, Any]) -> bool:
        lease = uow.get("executor_leases", lease_id, lock=True)
        if lease["state"] not in LIVE_LEASE_STATES:
            return False
        # Recorded even when a release already fenced the lease, so it can terminate it.
        uow.update(
            "executor_leases",
            lease_id,
            {"handle": handle, "observed_status": "allocated", "observed_at": uow.now()},
        )
        return lease["state"] == "allocating"

    def _lose_unbound(self, uow: Any, lease_id: str, reason: str) -> None:
        """Permanent allocation failure: isolation is only confirmed if nothing was created."""
        lease = uow.get("executor_leases", lease_id, lock=True)
        never_requested = lease["handle"] is None and lease["observed_status"] is None
        self._lose_lease(uow, lease_id, reason, confirmed=never_requested)
        if not never_requested:
            self._enqueue_release(uow, lease, "quarantine")

    def _enqueue_release(self, uow: Any, lease: dict[str, Any], reason: str) -> None:
        uow.enqueue_job(
            workspace_id=lease["workspace_id"],
            kind="executor.release",
            target_id=lease["id"],
            session_id=lease["session_id"],
            input={"reason": reason},
        )

    def _lose_lease(self, uow: Any, lease_id: str, reason: str, *, confirmed: bool) -> None:
        lease = uow.get("executor_leases", lease_id, lock=True)
        if lease["state"] in ("released", "lost"):
            if confirmed and lease["quarantined"]:
                uow.update("executor_leases", lease_id, {"quarantined": False})
            return
        uow.update(
            "executor_leases",
            lease_id,
            {
                "state": "lost",
                "state_reason": reason,
                "quarantined": not confirmed,
                "released_at": uow.now() if confirmed else None,
            },
        )
        session = uow.get("sessions", lease["session_id"], lock=True)
        if session["active_lease_id"] == lease_id:
            uow.update("sessions", session["id"], {"active_lease_id": None})
        uow.update_where(
            "worktrees",
            {"session_id": session["id"], "availability": ["live", "restoring"]},
            {"availability": "unavailable", "updated_at": uow.now()},
        )
        uow.update_where(
            "capacity_reservations",
            {"lease_id": lease_id, "state": "active"},
            {"state": "released" if confirmed else "quarantined"},
        )
        uow.append_event(
            session,
            "executor.unavailable",
            {"lease_id": lease_id, "reason": reason, "isolation_confirmed": confirmed},
            actor="application",
            executor_lease_id=lease_id,
            lease_generation=lease["generation"],
        )

    # -------------------------------------------------------------------- worktree
    def _ensure_worktree(self, ctx: Any, lease: dict[str, Any]) -> None:
        def read(uow: Any) -> tuple[Any, Any, Any]:
            session = uow.get("sessions", lease["session_id"])
            worktree = uow.find_one("worktrees", {"session_id": session["id"]})
            snap = (
                uow.get("snapshots", worktree["last_snapshot_id"])
                if worktree["last_snapshot_id"]
                else None
            )
            return session, worktree, snap

        session, worktree, snapshot = ctx.db.read(read)
        realized = (worktree["recovery_point"] or {}).get("lease_id") == lease["id"]
        if worktree["availability"] == "live" and realized:
            return
        payload: dict[str, Any] = {"generation": worktree["generation"]}
        secrets: dict[str, Any] = runtime_visible(self.credentials, session)
        repo = session["effective_spec"].get("repository")
        if snapshot is not None and snapshot["state"] == "ready":
            # A valid Session checkpoint takes precedence; wake never resets to main.
            payload["checkpoint_b64"] = base64.b64encode(
                self.blobs.get(snapshot["backend_ref"])
            ).decode()
            payload["snapshot_id"] = snapshot["id"]
        elif repo:
            payload["repository"] = {
                "clone_url": repo.get("clone_url") or f"https://github.com/{repo['full_name']}.git",
                "base_ref": repo.get("base_ref") or "main",
                "base_sha": worktree["base_sha"],
            }
            source = self.credentials.source(session)
            if source:
                secrets["git"] = source
        result = self.connector.channel(lease).op(
            "worktree.restore", f"{lease['id']}:restore", session["id"], payload, secrets=secrets
        )
        if result["status"] != "succeeded":
            error = (result.get("result") or {}).get("error") or {}
            raise DomainError(
                "executor_unavailable",
                f"worktree restore failed: {error.get('message', 'unknown')}",
                retryable=False,
            )
        restored = result["result"]

        def commit(uow: Any) -> None:
            row = uow.get("worktrees", worktree["id"], lock=True)
            values = {
                "availability": "live",
                "recovery_point": {
                    "lease_id": lease["id"],
                    "restored_from": restored["restored_from"],
                    "snapshot_id": payload.get("snapshot_id"),
                },
                "updated_at": uow.now(),
            }
            if not row["base_sha"]:
                values["base_sha"] = restored["base_sha"]
            uow.update("worktrees", row["id"], values)
            session_row = uow.get("sessions", session["id"], lock=True)
            uow.append_event(
                session_row,
                "worktree.restored",
                {
                    "worktree_id": row["id"],
                    "generation": row["generation"],
                    "base_sha": values.get("base_sha", row["base_sha"]),
                    "restored_from": restored["restored_from"],
                    "snapshot_id": payload.get("snapshot_id"),
                },
                actor="application",
                executor_lease_id=lease["id"],
                lease_generation=lease["generation"],
            )

        ctx.commit(commit)
        for hook in self.post_restore_hooks:
            hook(ctx, lease, session)

    # -------------------------------------------------------------------- execution
    def _create_execution(self, uow: Any, turn_id: str, lease_id: str) -> str | None:
        turn = uow.get("turns", turn_id)
        session = uow.get("sessions", turn["session_id"], lock=True)
        turn = uow.get("turns", turn_id, lock=True)
        if turn["state"] != "preparing":
            return None
        lease = uow.get("executor_leases", lease_id, lock=True)
        if lease["state"] != "ready":
            return "wait"
        connection_id = session["inference_connection_id"]
        slot = None
        if connection_id:
            connection = uow.get("connections", connection_id, lock=True)
            used = {
                r["slot_ordinal"]
                for r in uow.query("capacity.active_slots", connection_id=connection_id)
            }
            free = [i for i in range(connection["slot_limit"]) if i not in used]
            if not free:
                if turn["reason"] != "waiting_capacity":
                    uow.update("turns", turn_id, {"reason": "waiting_capacity"})
                    uow.append_event(
                        session,
                        "turn.preparing",
                        {"reason": "waiting_capacity"},
                        actor="application",
                        turn_id=turn_id,
                    )
                return "wait"
            slot = free[0]
        attempt = uow.count("executions", {"turn_id": turn_id}) + 1
        execution_id = new_id("execution")
        meta = self.credentials.check_inference(uow, session)
        uow.insert(
            "executions",
            {
                "id": execution_id,
                "workspace_id": session["workspace_id"],
                "session_id": session["id"],
                "turn_id": turn_id,
                "attempt_ordinal": attempt,
                "executor_lease_id": lease_id,
                "operation_id": execution_id,
                "harness_provider": session["harness_provider"],
                "inference_connection_id": connection_id,
                "credential_version_id": meta.get("credential_version_id"),
                "image_digest": lease["image_digest"],
                "runtime_epoch": lease["runtime_epoch"],
            },
        )
        if slot is not None:
            uow.insert(
                "capacity_reservations",
                {
                    "id": new_id("reservation"),
                    "workspace_id": session["workspace_id"],
                    "connection_id": connection_id,
                    "slot_ordinal": slot,
                    "execution_id": execution_id,
                    "lease_id": lease_id,
                },
            )
        if turn["reason"] == "waiting_capacity":
            uow.update("turns", turn_id, {"reason": None})
        uow.append_event(
            session,
            "execution.preparing",
            {
                "attempt": attempt,
                "lease_id": lease_id,
                "provider_id": session["harness_provider"],
                "credential_version_id": meta.get("credential_version_id"),
            },
            actor="application",
            turn_id=turn_id,
            execution_id=execution_id,
            executor_lease_id=lease_id,
            lease_generation=lease["generation"],
        )
        return execution_id

    def _start_payload(self, uow: Any, execution: dict[str, Any]) -> tuple[Any, Any, Any]:
        turn = uow.get("turns", execution["turn_id"])
        session = uow.get("sessions", execution["session_id"])
        message = uow.get("messages", turn["triggering_message_id"])
        lease = uow.get("executor_leases", execution["executor_lease_id"])
        binding = uow.find_one(
            "native_context_bindings",
            {"session_id": session["id"], "provider_id": session["harness_provider"]},
            order="created_at DESC",
        )
        settings = turn["settings"] or {}
        payload = {
            "provider_id": session["harness_provider"],
            "turn_id": turn["id"],
            "execution_id": execution["id"],
            "prompt": compose_prompt(message, turn),
            "model": settings.get("model") or session["harness_model"],
            "effort": settings.get("effort"),
            "native_binding": {
                "provider_id": binding["provider_id"],
                "native_id": binding["native_id"],
            }
            if binding
            else None,
            "deadline_seconds": self.settings.turn_deadline_seconds,
        }
        return payload, session, lease

    def _start(self, ctx: Any, execution_id: str) -> None:
        execution = ctx.db.read(lambda uow: uow.get("executions", execution_id))
        payload, session, lease = ctx.db.read(lambda uow: self._start_payload(uow, execution))
        accepted = False
        refused: RuntimeRefused | None = None
        try:
            bundle, _meta = self.credentials.inference(session)
            response = self.connector.channel(lease).op(
                "turn.start", execution["operation_id"], session["id"], payload, secrets=bundle
            )
            accepted = response["status"] in (
                "accepted",
                "starting",
                "started",
                "succeeded",
                "failed",
            )
        except RuntimeUnavailable:
            pass  # lost start response: reconcile inspects the same operation, never blind replay
        except RuntimeRefused as exc:
            refused = exc

        def commit(uow: Any) -> None:
            row = uow.get("executions", execution_id, lock=True)
            if refused is not None and row["state"] == "preparing":
                uow.update(
                    "executions",
                    execution_id,
                    {
                        "state": "failed",
                        "outcome": {"refused": refused.code},
                        "finished_at": uow.now(),
                    },
                )
                uow.update_where(
                    "capacity_reservations",
                    {"execution_id": execution_id, "state": "active"},
                    {"state": "released", "released_at": uow.now()},
                )
                if refused.code == "busy":
                    uow.enqueue_job(
                        workspace_id=row["workspace_id"],
                        kind="turn.dispatch",
                        target_id=row["turn_id"],
                        dedupe_key=f"{row['turn_id']}:after:{execution_id}",
                        session_id=row["session_id"],
                    )
                else:
                    self._fail_preparing(uow, row["turn_id"], refused.code, str(refused))
                return
            if accepted and row["launch_evidence"] == "none":
                uow.update("executions", execution_id, {"launch_evidence": "accepted"})
            uow.enqueue_job(
                workspace_id=row["workspace_id"],
                kind="execution.reconcile",
                target_id=execution_id,
                session_id=row["session_id"],
            )

        ctx.commit(commit)

    # -------------------------------------------------------------------- reconcile
    def handle_reconcile(self, ctx: Any) -> Outcome:
        execution_id = ctx.claim.job["execution_id"]

        def read(uow: Any) -> tuple[Any, Any, Any]:
            execution = uow.get("executions", execution_id)
            lease = uow.get("executor_leases", execution["executor_lease_id"])
            return execution, lease, uow.get("turns", execution["turn_id"])

        execution, lease, turn = ctx.db.read(read)
        if execution["state"] not in LIVE_EXECUTION_STATES and turn["state"] in _TERMINAL:
            return Succeeded({"state": execution["state"]})
        if lease["state"] in ("released", "lost"):
            ctx.commit(
                lambda uow: self._runtime_lost(
                    uow, execution_id, confirmed=not lease["quarantined"]
                )
            )
            return Succeeded({"lost": True})
        channel = self.connector.channel(lease)
        unreachable = int(ctx.input.get("unreachable") or 0)
        try:
            if turn["state"] == "cancelling" or ctx.input.get("intent") == "cancel":
                self._request_stop(ctx, channel, execution)
            if execution["launch_evidence"] == "none":
                status = channel.query("operation.status", operation_id=execution["operation_id"])
                if status["status"] == "unknown":
                    if turn["state"] == "cancelling":
                        ctx.commit(lambda uow: self._seal_unlaunched(uow, execution_id))
                        return Succeeded({"cancelled_before_launch": True})
                    # Never accepted by the runtime, so it never launched: safe to resend.
                    self._start(ctx, execution_id)
                    return Continue(delay=self.settings.poll_busy, input={"unreachable": 0})
            acked = ctx.db.read(lambda uow: self._acked(uow, lease))
            batch = channel.events(acked)
            if batch["runtime_epoch"] != lease["runtime_epoch"]:
                # A new empty incarnation: evidence after our watermark is lost.
                ctx.commit(
                    lambda uow: self._runtime_lost(
                        uow, execution_id, confirmed=False, reason="runtime_epoch_changed"
                    )
                )
                return Succeeded({"epoch_changed": True})
            result = ctx.commit(
                lambda uow: ingest(uow, lease, batch["runtime_epoch"], batch["items"], self.hooks)
            )
            if result.acked > acked:
                channel.ack(batch["runtime_epoch"], result.acked)
            if result.terminal:
                return Succeeded({"acked": result.acked})
            more = batch["last_local_seq"] > result.acked
            delay = (
                0
                if more
                else (self.settings.poll_busy if batch["items"] else self.settings.poll_idle)
            )
            return Continue(delay=delay, input={"unreachable": 0, "intent": None})
        except RuntimeUnavailable:
            unreachable += 1
            if unreachable < self.settings.unreachable_threshold:
                return Continue(
                    delay=min(5.0, 0.2 * unreachable), input={"unreachable": unreachable}
                )
            session = ctx.db.read(lambda uow: uow.get("sessions", lease["session_id"]))
            try:
                status = (
                    self._backend(lease)
                    .describe(lease["handle"] or {}, self._compute(session))
                    .get("status")
                )
            except Exception:
                status = "unknown"
            confirmed = status == "terminated"
            ctx.commit(lambda uow: self._runtime_lost(uow, execution_id, confirmed=confirmed))
            return Succeeded({"lost": True, "isolation_confirmed": confirmed})

    def _acked(self, uow: Any, lease: dict[str, Any]) -> int:
        row = uow.find_one(
            "runtime_ingestion_offsets",
            {"executor_lease_id": lease["id"], "runtime_epoch": lease["runtime_epoch"]},
        )
        return int(row["acked_local_seq"]) if row else 0

    def _request_stop(self, ctx: Any, channel: Any, execution: dict[str, Any]) -> None:
        if execution["state"] not in ("preparing", "started", "stop_requested"):
            return
        channel.op(
            "turn.cancel",
            f"{execution['operation_id']}:cancel",
            execution["session_id"],
            {"target_operation_id": execution["operation_id"]},
        )

        def commit(uow: Any) -> None:
            row = uow.get("executions", execution["id"], lock=True)
            if row["state"] == "started":
                uow.update("executions", row["id"], {"state": "stop_requested"})

        ctx.commit(commit)

    def _seal_unlaunched(self, uow: Any, execution_id: str) -> None:
        execution = uow.get("executions", execution_id, lock=True)
        session = uow.get("sessions", execution["session_id"], lock=True)
        turn = uow.get("turns", execution["turn_id"], lock=True)
        uow.update("executions", execution_id, {"state": "cancelled", "finished_at": uow.now()})
        uow.update_where(
            "capacity_reservations",
            {"execution_id": execution_id, "state": "active"},
            {"state": "released", "released_at": uow.now()},
        )
        if turn["state"] == "cancelling":
            finish_turn(
                uow,
                session,
                turn,
                "cancelled",
                actor="application",
                reason="cancelled_by_user",
                evidence_complete=True,
                outcome={"started": False},
                execution_id=execution_id,
            )

    def _runtime_lost(
        self, uow: Any, execution_id: str, *, confirmed: bool, reason: str = "runtime_unavailable"
    ) -> None:
        """Runtime vanished before final evidence: history kept, outcome unknown (A14)."""
        execution = uow.get("executions", execution_id, lock=True)
        session = uow.get("sessions", execution["session_id"], lock=True)
        turn = uow.get("turns", execution["turn_id"], lock=True)
        self._lose_lease(uow, execution["executor_lease_id"], reason, confirmed=confirmed)
        if execution["state"] in LIVE_EXECUTION_STATES:
            uow.update(
                "executions",
                execution_id,
                {
                    "state": "unknown",
                    "finished_at": uow.now(),
                    "outcome": {"verdict": "unknown", "reason": reason},
                },
            )
        uow.update_where(
            "capacity_reservations",
            {"execution_id": execution_id, "state": "active"},
            {"state": "released" if confirmed else "quarantined"},
        )
        if turn["state"] not in _TERMINAL:
            if turn["state"] == "cancelling":
                state = "cancelled" if confirmed else "interrupted"
                why = "cancelled_by_user" if confirmed else "outcome_unknown"
            elif turn["state"] == "running":
                state, why = "interrupted", "outcome_unknown"
            else:
                state, why = "failed", "outcome_unknown"
            finish_turn(
                uow,
                session,
                turn,
                state,
                actor="application",
                reason=why,
                error_code=None if why == "cancelled_by_user" else "outcome_unknown",
                error_message="runtime disappeared before final evidence",
                evidence_complete=False,
                execution_id=execution_id,
            )
        if not confirmed:
            self._enqueue_release(
                uow, uow.get("executor_leases", execution["executor_lease_id"]), "quarantine"
            )

    # -------------------------------------------------------------- allocate/release
    def handle_allocate(self, ctx: Any) -> Outcome:
        lease_id = ctx.claim.job["lease_id"]
        try:
            lease = self._ensure_lease(ctx, lease_id)
            if lease is None:
                return Succeeded({"state": "not_allocating"})
            self._ensure_worktree(ctx, lease)
        except RuntimeUnavailable as exc:
            return Retry("executor_unavailable", str(exc)[:200])
        return Succeeded({"lease_id": lease_id})

    def handle_release(self, ctx: Any) -> Outcome:
        lease_id = ctx.claim.job["lease_id"]
        lease = ctx.db.read(lambda uow: uow.get("executor_leases", lease_id))
        session = ctx.db.read(lambda uow: uow.get("sessions", lease["session_id"]))
        if lease["state"] in ("released", "lost") and not lease["quarantined"]:
            return Succeeded({"state": lease["state"]})
        snapshot_ok = False
        if lease["state"] in ("ready", "quiescing") and not lease["quarantined"]:
            busy = ctx.db.read(
                lambda uow: uow.count(
                    "executions",
                    {"executor_lease_id": lease_id, "state": list(LIVE_EXECUTION_STATES)},
                )
            )
            capturing = ctx.db.read(
                lambda uow: uow.count(
                    "changesets", {"session_id": lease["session_id"], "state": "capturing"}
                )
            )
            if busy or capturing:
                return Continue(delay=2.0)
            ctx.commit(lambda uow: self._quiesce(uow, lease_id))
            snapshot_ok = self._checkpoint(ctx, lease)
        # Fence an allocating lease first: no new backend create can start after this.
        lease = ctx.commit(lambda uow: self._quiesce(uow, lease_id))
        confirmed = self._confirm_stopped(lease, session)
        if confirmed is None:
            return Continue(delay=5.0)
        if not confirmed:
            ctx.commit(lambda uow: self._quarantine(uow, lease_id, "termination_unconfirmed"))
            return Retry("executor_unavailable", "termination not confirmed")
        ctx.commit(lambda uow: self._released(uow, lease_id, snapshot_ok))
        return Succeeded({"checkpoint": snapshot_ok})

    def _confirm_stopped(self, lease: dict[str, Any], session: dict[str, Any]) -> bool | None:
        """True only on confirmed termination or authoritative absence; None = wait.

        A missing handle never implies cleanup: the allocation is resolved by its
        operation identity, and lookup failures stay unconfirmed (quarantined).
        """
        try:
            backend = self._backend(lease)
            compute = self._compute(session)
            handle = lease["handle"]
            if not handle:
                found = backend.lookup(lease["allocation_operation_id"], compute)
                if found is None:
                    observed = lease["observed_at"]
                    in_flight = (
                        lease["observed_status"] == "allocate_requested"
                        and observed is not None
                        and (self._now() - observed).total_seconds()
                        < self.settings.allocation_window_seconds
                    )
                    return None if in_flight else True
                if found.get("status") == "terminated":
                    return True
                handle = {k: v for k, v in found.items() if k != "status"}
            return bool(backend.terminate(handle, f"{lease['id']}:terminate", compute))
        except Exception:
            return False

    def _now(self) -> Any:
        return self.tx.read(lambda uow: uow.now())

    def _quarantine(self, uow: Any, lease_id: str, reason: str) -> None:
        lease = uow.get("executor_leases", lease_id, lock=True)
        if lease["quarantined"]:
            return
        uow.update("executor_leases", lease_id, {"quarantined": True, "state_reason": reason})
        session = uow.get("sessions", lease["session_id"], lock=True)
        uow.append_event(
            session,
            "executor.unavailable",
            {"lease_id": lease_id, "reason": reason, "isolation_confirmed": False},
            actor="application",
            executor_lease_id=lease_id,
            lease_generation=lease["generation"],
        )

    def _quiesce(self, uow: Any, lease_id: str) -> dict[str, Any]:
        lease = uow.get("executor_leases", lease_id, lock=True)
        if lease["state"] in ("ready", "allocating"):
            lease = uow.update("executor_leases", lease_id, {"state": "quiescing"})
            session = uow.get("sessions", lease["session_id"], lock=True)
            uow.append_event(
                session,
                "executor.quiescing",
                {"lease_id": lease_id},
                actor="application",
                executor_lease_id=lease_id,
                lease_generation=lease["generation"],
            )
        return lease

    def _checkpoint(self, ctx: Any, lease: dict[str, Any]) -> bool:
        """Barrier, drain/ack evidence, export secret-free files + native state, seal."""
        worktree = ctx.db.read(
            lambda uow: uow.find_one("worktrees", {"session_id": lease["session_id"]})
        )
        if worktree["availability"] != "live":
            return False
        channel = self.connector.channel(lease)
        snapshot_id = new_id("snapshot")
        try:
            acked = ctx.db.read(lambda uow: self._acked(uow, lease))
            batch = channel.events(acked)
            if batch["items"]:
                result = ctx.commit(
                    lambda uow: ingest(
                        uow, lease, batch["runtime_epoch"], batch["items"], self.hooks
                    )
                )
                channel.ack(batch["runtime_epoch"], result.acked)
            session = ctx.db.read(lambda uow: uow.get("sessions", lease["session_id"]))
            response = channel.op(
                "snapshot.prepare",
                f"{lease['id']}:checkpoint",
                lease["session_id"],
                {},
                secrets=runtime_visible(self.credentials, session),
            )
        except (RuntimeUnavailable, RuntimeRefused) as exc:
            ctx.commit(
                lambda uow, e=exc: self._snapshot_failed(uow, lease, snapshot_id, str(e)[:200])
            )
            return False
        if response["status"] != "succeeded":
            error = str((response.get("result") or {}).get("error"))[:200]
            ctx.commit(lambda uow: self._snapshot_failed(uow, lease, snapshot_id, error))
            return False
        data = base64.b64decode(response["result"]["checkpoint_b64"])
        digest = "sha256:" + sha256_hex(data)
        if digest != response["result"]["content_digest"]:
            ctx.commit(
                lambda uow: self._snapshot_failed(uow, lease, snapshot_id, "digest mismatch")
            )
            return False
        key = f"{lease['workspace_id']}/checkpoints/{snapshot_id}"
        self.blobs.put(key, data)

        def seal(uow: Any) -> None:
            wt = uow.get("worktrees", worktree["id"], lock=True)
            session = uow.get("sessions", lease["session_id"], lock=True)
            uow.insert(
                "snapshots",
                {
                    "id": snapshot_id,
                    "workspace_id": lease["workspace_id"],
                    "kind": "checkpoint",
                    "worktree_id": wt["id"],
                    "worktree_generation": wt["generation"],
                    "content_digest": digest,
                    "backend_ref": key,
                    "manifest": {
                        "size": len(data),
                        "native_state": True,
                        "format": "tar.gz/v1",
                        # Paths withheld by the artifact secret policy (names only).
                        "secret_excluded": response["result"].get("excluded", []),
                    },
                    "compatibility": {
                        "image_digest": lease["image_digest"],
                        "protocol": lease["protocol_version"],
                    },
                    "event_watermark": uow.watermark(session["id"]),
                },
            )
            uow.update("snapshots", snapshot_id, {"state": "ready", "sealed_at": uow.now()})
            uow.update("worktrees", wt["id"], {"last_snapshot_id": snapshot_id})
            uow.append_event(
                session,
                "snapshot.ready",
                {
                    "snapshot_id": snapshot_id,
                    "kind": "checkpoint",
                    "generation": wt["generation"],
                    "content_digest": digest,
                },
                actor="application",
                executor_lease_id=lease["id"],
            )

        ctx.commit(seal)
        return True

    def _snapshot_failed(
        self, uow: Any, lease: dict[str, Any], snapshot_id: str, error: str
    ) -> None:
        session = uow.get("sessions", lease["session_id"], lock=True)
        uow.append_event(
            session,
            "snapshot.failed",
            {"snapshot_id": snapshot_id, "kind": "checkpoint", "error": error},
            actor="application",
            executor_lease_id=lease["id"],
        )

    def _released(self, uow: Any, lease_id: str, snapshot_ok: bool) -> None:
        lease = uow.get("executor_leases", lease_id, lock=True)
        session = uow.get("sessions", lease["session_id"], lock=True)
        if lease["state"] in ("ready", "quiescing", "allocating"):
            if lease["state"] != "quiescing":
                uow.update("executor_leases", lease_id, {"state": "quiescing"})
            uow.update(
                "executor_leases",
                lease_id,
                {"state": "released", "released_at": uow.now(), "quarantined": False},
            )
        else:
            uow.update("executor_leases", lease_id, {"quarantined": False})
        uow.update_where(
            "capacity_reservations",
            {"lease_id": lease_id, "state": ["active", "quarantined"]},
            {"state": "released", "released_at": uow.now()},
        )
        if session["active_lease_id"] == lease_id:
            uow.update("sessions", session["id"], {"active_lease_id": None})
        worktree = uow.find_one("worktrees", {"session_id": session["id"]}, lock=True)
        availability = "checkpointed" if (snapshot_ok or worktree["last_snapshot_id"]) else "none"
        uow.update(
            "worktrees", worktree["id"], {"availability": availability, "updated_at": uow.now()}
        )
        uow.append_event(
            session,
            "executor.released",
            {"lease_id": lease_id, "checkpoint": snapshot_ok},
            actor="application",
            executor_lease_id=lease_id,
            lease_generation=lease["generation"],
        )
