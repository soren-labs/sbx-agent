"""Execution application — admission, dispatch, adopt, reconcile (RFC 167 §03).

The dispatch path: turn.dispatch Job → lock Turn → create Execution +
output Message → ensure a live ExecutorLease (allocate → enroll → attach)
→ submit turn.start/turn.resume to sbx-runtime → mark started. Terminal
verdicts come back through ``settle_operation_result`` (operation.result
frame) and runtime evidence ingestion; reads never settle work.
"""

from __future__ import annotations

import threading
from typing import Any

from protocol.runtime import OperationEnvelope, OperationKind

from control.domain import ids
from control.domain.errors import DomainError, NotFound
from control.domain.execution import ExecutionState, LeaseState
from control.domain.sessions import TurnState, require_turn_transition
from control.persistence.base import _now
from control.persistence.unit_of_work import SqlUnitOfWork

from .events import append_event, ingest_runtime_events
from .ingest import observations_to_records, project_observation

ATTACH_TIMEOUT_S = 30.0
# Serve-mode runtimes need a longer window: executor image pull + daemon
# boot + tunnel accept routinely exceed the post-connect hello delay.
DIAL_TIMEOUT_S = 180.0
SUBMIT_TIMEOUT_S = 30.0


class ExecutionService:
    """Dispatch and reconcile Turns on ExecutorLeases.

    ``db`` + ``backends`` (kind → ExecutorBackend) + ``pool`` (RuntimePool)
    are wired at composition; credentials arrive as opaque payload bundles
    resolved by ``credential_resolver(execution, session) -> dict``.
    """

    def __init__(
        self,
        db,
        backends: dict[str, Any],
        pool: Any,
        credential_resolver=None,
        git_resolver=None,
        compute_resolver=None,
    ) -> None:
        self.db = db
        self.backends = backends
        self.pool = pool
        self.credential_resolver = credential_resolver or (lambda session, turn: {})
        # git_resolver: workspace's git Connection → clone/fetch creds for
        # worktree materialization inside the executor.
        self.git_resolver = git_resolver or (lambda session, **kw: {"env": {}})
        # compute_resolver: workspace's modal Connection → executor backend
        # creds bound to this call (never ambient fallback).
        self.compute_resolver = compute_resolver or (lambda session, **kw: {})
        self._pending_results: dict[str, dict] = {}
        self._pending_lock = threading.Lock()

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------
    def dispatch_turn(self, uow: SqlUnitOfWork, *, workspace_id: str, turn_id: str) -> dict:
        """The turn.dispatch handler body. Runs inside the job's UoW; the
        commit lands allocation+execution atomically, then we spawn/attach
        out-of-band work — a lost response is recovered by lookup, never by
        double-allocation."""
        turn = uow.turns.get_for_update(workspace_id, turn_id)
        if turn is None:
            raise NotFound("turn")
        session = uow.sessions.get_for_update(workspace_id, turn["session_id"])
        if session is None:
            raise NotFound("session")
        if turn["state"] not in (TurnState.QUEUED.value, TurnState.PREPARING.value):
            return {"turn_id": turn_id, "skipped": turn["state"]}

        # Worktree apply barrier: a pending changeset.apply (e.g. a staged
        # delegation input) fences the worktree — turns wait rather than
        # race it. JobRetry keeps the turn queued until the barrier clears.
        wt = uow.worktrees.get_by_session(workspace_id, session["id"])
        if wt is not None:
            active = uow.rows.one(
                "SELECT count(*) AS n FROM worktree_operations"
                " WHERE worktree_id=%s AND state='active'",
                (wt["id"],),
            )
            if active and int(active["n"]) > 0:
                from control.jobs.worker import JobRetry

                raise JobRetry("worktree apply in flight", retry_after=25.0)

        # Idempotent resume: a PREPARING turn with an existing Execution is
        # a job retry — reuse the row (and its operation_id effect identity)
        # instead of double-allocating.
        execution = uow.executions.nonterminal_by_turn(workspace_id, turn_id)
        if turn["state"] == TurnState.QUEUED.value or execution is None:
            require_turn_transition(TurnState(turn["state"]), TurnState.PREPARING)
            if turn["state"] == TurnState.QUEUED.value:
                uow.turns.update(
                    workspace_id,
                    turn_id,
                    {"state": TurnState.PREPARING.value},
                    expected_version=turn["version"],
                )
                append_event(
                    uow,
                    workspace_id=workspace_id,
                    session_id=session["id"],
                    event_type="turn.preparing",
                    turn_id=turn_id,
                    payload={"turn_id": turn_id},
                )

            # The assistant output message carries this turn's parts.
            out_ordinal = uow.sessions.allocate(workspace_id, session["id"], "next_message_ordinal")
            output_message_id = ids.new_id("message")
            uow.messages.insert(
                {
                    "id": output_message_id,
                    "workspace_id": workspace_id,
                    "session_id": session["id"],
                    "ordinal": out_ordinal,
                    "author": {"kind": "session", "session_id": session["id"]},
                    "role": "assistant",
                    "routing": "note",
                    "content": {"kind": "text"},
                    "routed_turn_id": turn_id,
                }
            )
            uow.turns.update(
                workspace_id,
                turn_id,
                {
                    "resolved_settings": {
                        **turn.get("resolved_settings", {}),
                        "output_message_id": output_message_id,
                    }
                },
            )

            # The Execution is the attempt record; operation_id is its
            # once-only effect identity.
            attempt = uow.executions.next_ordinal(workspace_id, turn_id)
            operation_id = ids.new_id("effect")
            execution_id = ids.new_id("execution")
            uow.executions.insert(
                {
                    "id": execution_id,
                    "workspace_id": workspace_id,
                    "session_id": session["id"],
                    "turn_id": turn_id,
                    "attempt_ordinal": attempt,
                    "operation_id": operation_id,
                    "state": ExecutionState.PREPARING.value,
                }
            )
            uow.conn.execute(
                "UPDATE messages SET routed_execution_id=%s WHERE id=%s",
                (execution_id, output_message_id),
            )
            execution = {
                "id": execution_id,
                "attempt_ordinal": attempt,
                "operation_id": operation_id,
                "executor_lease_id": None,
            }

        execution_id = execution["id"]
        attempt = execution["attempt_ordinal"]
        operation_id = execution["operation_id"]

        harness = session.get("harness") or {}
        provider_id = harness.get("provider_id") or "opencode"
        model = harness.get("model")
        message = uow.messages.get(workspace_id, turn["message_id"])
        prompt = ((message or {}).get("content") or {}).get("text") or ""

        # ExecutorLease: reuse a live attached one or allocate a fresh one.
        lease = self._ensure_lease(uow, session=session)

        # Native binding for resume — valid only while the SAME lease's
        # daemon holds the native state; a reallocated lease starts fresh
        # (cross-lease restore is the Phase 4 checkpoint path).
        binding = self._resumable_binding(
            uow,
            workspace_id=workspace_id,
            session_id=session["id"],
            provider_id=provider_id,
            lease_id=lease["id"],
        )

        uow.executions.update(
            workspace_id,
            execution_id,
            {
                "executor_lease_id": lease["id"],
                "lease_generation": lease["generation"],
                "native_binding_id": binding["id"] if binding else None,
            },
        )
        uow.sessions.update(
            workspace_id,
            session["id"],
            {"active_turn_id": turn_id, "active_lease_id": lease["id"]},
        )
        # Commit before the slow path: allocation + execution are durable,
        # and the lease row must exist for the daemon's hello to verify.
        uow.commit()

        # --- out-of-band: spawn/attach, submit, mark started -----------
        if lease.get("_spawned") is None:
            lease = self._spawn_and_attach(uow, session=session, lease=lease)
        try:
            credentials = self.credential_resolver(
                session,
                turn,
                execution_id=execution_id,
                lease_id=lease["id"],
                workspace_id=workspace_id,
            )
        except DomainError as exc:
            # Credential authority refused (no configured connection, revoked
            # grant, ...) — settle the turn as failed; there is no fallback.
            self._fail_execution(
                uow,
                workspace_id=workspace_id,
                session_id=session["id"],
                turn_id=turn_id,
                execution_id=execution_id,
                reason=exc.code,
                error={"code": exc.code, "message": str(exc)},
            )
            return {"turn_id": turn_id, "error": exc.code}
        worktree_spec = self._worktree_spec(uow, session)
        git_credentials = {}
        if worktree_spec:
            try:
                git_credentials = (
                    self.git_resolver(
                        session,
                        workspace_id=workspace_id,
                        lease_id=lease["id"],
                    )
                    or {}
                )
            except DomainError as exc:
                self._fail_execution(
                    uow,
                    workspace_id=workspace_id,
                    session_id=session["id"],
                    turn_id=turn_id,
                    execution_id=execution_id,
                    reason=exc.code,
                    error={"code": exc.code, "message": str(exc)},
                )
                return {"turn_id": turn_id, "error": exc.code}
        envelope = self._turn_envelope(
            lease=lease,
            session=session,
            turn=turn,
            execution={
                "id": execution_id,
                "attempt_ordinal": attempt,
                "operation_id": operation_id,
            },
            provider_id=provider_id,
            model=model,
            prompt=prompt,
            credentials=credentials,
            binding=binding,
            worktree=worktree_spec,
            git_credentials=git_credentials,
        )
        reply = self.pool.submit(lease["id"], envelope.to_dict(), timeout=SUBMIT_TIMEOUT_S)
        if reply.get("frame") == "operation.rejected":
            error = reply.get("error") or {}
            self._fail_execution(
                uow,
                workspace_id=workspace_id,
                session_id=session["id"],
                turn_id=turn_id,
                execution_id=execution_id,
                reason=str(error.get("code") or "rejected"),
                error=error,
            )
            return {"turn_id": turn_id, "rejected": error}

        # Conditional marks — evidence may already have settled this row
        # (a fast CLI can finish before we commit 'started').
        uow.conn.execute(
            "UPDATE executions SET state=%s, updated_at=now() WHERE id=%s AND state=%s",
            (ExecutionState.STARTED.value, execution_id, ExecutionState.PREPARING.value),
        )
        t = uow.turns.get_for_update(workspace_id, turn_id)
        if t is not None and t["state"] == TurnState.PREPARING.value:
            uow.turns.update(
                workspace_id,
                turn_id,
                {"state": TurnState.RUNNING.value},
                expected_version=t["version"],
            )
        append_event(
            uow,
            workspace_id=workspace_id,
            session_id=session["id"],
            event_type="execution.started",
            turn_id=turn_id,
            execution_id=execution_id,
            executor_lease_id=lease["id"],
            lease_generation=lease["generation"],
            payload={"operation_id": operation_id},
        )
        uow.commit()
        return {
            "turn_id": turn_id,
            "execution_id": execution_id,
            "lease_id": lease["id"],
            "accepted": True,
        }

    def _turn_envelope(
        self,
        *,
        lease: dict,
        session: dict,
        turn: dict,
        execution: dict,
        provider_id: str,
        model: str | None,
        prompt: str,
        credentials: dict,
        binding: dict | None,
        worktree: dict | None = None,
        git_credentials: dict | None = None,
    ) -> OperationEnvelope:
        from control.runtime_client import grants

        grant_id, grant_expires = grants.grant_context(lease.get("handle") or {})
        payload = {
            "turn_id": turn["id"],
            "execution_id": execution["id"],
            "attempt_ordinal": execution["attempt_ordinal"],
            "provider_id": provider_id,
            "prompt": prompt,
            "model": model,
            "worktree_generation": 0,
            "credentials": credentials,
        }
        if worktree:
            payload["worktree"] = worktree
            payload["git_credentials"] = git_credentials or {}
        if binding is not None:
            payload["native_binding"] = {
                "provider_id": binding["provider_id"],
                "native_id": binding["native_id"],
                "lineage_id": binding["lineage_id"],
                "cli_version": binding.get("cli_version"),
                "adapter_version": binding.get("adapter_version"),
                "state_manifest_digest": binding.get("state_manifest_digest"),
            }
        return OperationEnvelope(
            operation_id=execution["operation_id"],
            operation_kind=OperationKind.TURN_RESUME
            if binding is not None
            else OperationKind.TURN_START,
            session_id=session["id"],
            lease_id=lease["id"],
            lease_generation=lease["generation"],
            grant_id=grant_id,
            grant_expires_at=grant_expires,
            payload=payload,
        )

    def _worktree_spec(self, uow: SqlUnitOfWork, session: dict) -> dict | None:
        """The worktree's declared repository identity: projectless spec or
        the bound ProjectVersion's declaration. ``branch`` is derived from
        the session so a re-spawn resumes the same work branch."""
        spec = session.get("projectless_spec") or {}
        repo = spec.get("repository")
        base_ref = spec.get("base_ref")
        if not repo and session.get("project_version_id"):
            pv = uow.project_versions.get(session["workspace_id"], session["project_version_id"])
            decl = pv or {}
            repo = repo or decl.get("repository")
            base_ref = base_ref or decl.get("base_ref")
        if not repo:
            return None
        return {
            "repository": repo,
            "base_ref": base_ref or "main",
            "branch": f"sbx/{session['id']}",
        }

    def _latest_binding(
        self, uow: SqlUnitOfWork, workspace_id: str, session_id: str, provider_id: str
    ) -> dict | None:
        return uow.rows.one(
            "SELECT * FROM native_context_bindings WHERE workspace_id=%s"
            " AND session_id=%s AND provider_id=%s ORDER BY created_at DESC LIMIT 1",
            (workspace_id, session_id, provider_id),
        )

    def _ensure_lease(self, uow: SqlUnitOfWork, *, session: dict) -> dict:
        """Return a live attached lease or write a fresh ALLOCATING row.
        Actual backend spawn happens in ``_spawn_and_attach`` after commit —
        the daemon's hello verifies the lease row, which must be visible."""
        workspace_id = session["workspace_id"]
        lease = uow.leases.active_by_session(workspace_id, session["id"])
        if lease is not None:
            if self.pool.alive(lease["id"]):
                lease["_spawned"] = True
                return lease
            # Unverified old compute MUST be quarantined: mark lost and
            # terminate the backend handle before replacing it.
            self._mark_lease_lost(uow, lease)
            self._terminate_with_owner(uow, lease)
        generation = 1
        prev = uow.rows.one(
            "SELECT MAX(generation) AS g FROM executor_leases"
            " WHERE workspace_id=%s AND session_id=%s",
            (workspace_id, session["id"]),
        )
        if prev and prev["g"]:
            generation = int(prev["g"]) + 1
        from control.runtime_client import grants

        lease_id = ids.new_id("executor_lease")
        grant = grants.mint_grant(lease_id)
        allocation_op = ids.new_id("effect")
        pspec = session.get("projectless_spec") or {}
        backend_kind = pspec.get("executor_backend") or pspec.get("backend") or "local"
        if backend_kind not in self.backends:
            raise DomainError("validation_failed", f"unknown backend {backend_kind!r}")
        handle_meta = {"enrollment": grant.to_handle_record(), "backend": backend_kind}
        uow.leases.insert(
            {
                "id": lease_id,
                "workspace_id": workspace_id,
                "session_id": session["id"],
                "backend": backend_kind,
                "generation": generation,
                "state": LeaseState.ALLOCATING.value,
                "handle": handle_meta,
                "allocation_operation_id": allocation_op,
            }
        )
        append_event(
            uow,
            workspace_id=workspace_id,
            session_id=session["id"],
            event_type="executor.bound",
            payload={
                "lease_id": lease_id,
                "backend": backend_kind,
                "generation": generation,
            },
        )
        row = dict(uow.leases.get_for_update(workspace_id, lease_id) or {})
        row["_grant_token"] = grant.token  # plaintext lives only in memory
        return row

    def _spawn_and_attach(self, uow: SqlUnitOfWork, *, session: dict, lease: dict) -> dict:
        """Spawn the daemon for an ALLOCATING lease and wait for hello."""
        from control.executors.port import AllocationSpec

        backend = self.backends[lease["backend"]]
        token = lease.pop("_grant_token", None) or self._rotate_grant(uow, lease)
        connection = {}
        if lease["backend"] == "modal":
            # The USER's stored Modal credential bounds this call — never an
            # ambient profile (RFC 167 §06 credential authority).
            connection = self.compute_resolver(
                session,
                workspace_id=lease["workspace_id"],
                lease_id=lease["id"],
            )
        spec = AllocationSpec(
            workspace_id=lease["workspace_id"],
            session_id=session["id"],
            lease_id=lease["id"],
            allocation_effect_id=lease["allocation_operation_id"],
            lease_generation=lease["generation"],
            enrollment_ref=self.pool.endpoint(),
            connection=connection,
            env={"SBX_ENROLLMENT_TOKEN": token or ""},
        )
        from control.jobs.worker import JobRetry

        try:
            handle = backend.allocate(spec, lease["allocation_operation_id"])
        except Exception as exc:
            self._mark_lease_lost(uow, lease, spawn_failed=True)
            uow.commit()
            raise JobRetry(f"allocate failed: {type(exc).__name__}: {exc}") from exc
        dial_url = (handle.metadata or {}).get("dial_url")
        attached = True
        if dial_url:
            # Serve-mode runtime (Modal): the control plane dials in over
            # the sandbox's encrypted tunnel; hello arrives on that channel.
            attached = self.pool.dial(dial_url, retry_for_s=DIAL_TIMEOUT_S)
        if attached:
            attached = self.pool.attach(lease["id"], ATTACH_TIMEOUT_S)
        if not attached:
            self._mark_lease_lost(uow, lease, spawn_failed=True)
            uow.commit()
            try:
                backend.terminate(
                    handle,
                    lease["allocation_operation_id"],
                    connection=connection,
                )
            except Exception:
                pass
            raise JobRetry("daemon did not enroll within attach window")
        meta = dict(handle.metadata)
        meta["handle_id"] = handle.handle_id
        meta["enrollment"] = (lease.get("handle") or {}).get("enrollment")
        meta["backend"] = lease["backend"]
        uow.leases.update(
            lease["workspace_id"],
            lease["id"],
            {"state": LeaseState.READY.value, "handle": meta},
        )
        out = dict(lease)
        out["state"] = LeaseState.READY.value
        out["handle"] = meta
        out["_spawned"] = True
        return out

    def _rotate_grant(self, uow: SqlUnitOfWork, lease: dict) -> str:
        """Re-spawn path can't recover the minted plaintext — mint a new
        grant and store its digest (renewal requires current DB authority)."""
        from control.runtime_client import grants

        grant = grants.mint_grant(lease["id"])
        handle = dict(lease.get("handle") or {})
        handle["enrollment"] = grant.to_handle_record()
        uow.leases.update(lease["workspace_id"], lease["id"], {"handle": handle})
        uow.commit()  # digest must be visible before the daemon's hello
        return grant.token

    def _resumable_binding(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        session_id: str,
        provider_id: str,
        lease_id: str,
    ) -> dict | None:
        """Latest binding whose producing execution ran on THIS lease."""
        return uow.rows.one(
            "SELECT nb.* FROM native_context_bindings nb"
            " JOIN executions e ON e.native_binding_id = nb.id"
            " WHERE nb.workspace_id=%s AND nb.session_id=%s AND nb.provider_id=%s"
            " AND e.executor_lease_id=%s"
            " ORDER BY nb.created_at DESC LIMIT 1",
            (workspace_id, session_id, provider_id, lease_id),
        )

    # ------------------------------------------------------------------
    # inbound evidence / results
    # ------------------------------------------------------------------
    def ingest_batch(self, lease_row: dict, runtime_epoch: str, events: list[dict]) -> int:
        """Events-batch callback (runs on the ingress loop thread): commits
        deduped runtime events + projections, returns committed watermark."""
        with SqlUnitOfWork(self.db, actor={"kind": "runtime", "id": lease_row["id"]}) as uow:
            session_id = lease_row["session_id"]
            workspace_id = lease_row["workspace_id"]
            session = uow.sessions.get_for_update(workspace_id, session_id)
            if session is None:
                return 0
            # Resolve turn/execution context from any event or the active turn.
            active = uow.turns.active_by_session(workspace_id, session_id)
            turn_id = active["id"] if active else None
            execution = (
                uow.executions.nonterminal_by_turn(workspace_id, turn_id) if turn_id else None
            )
            execution_id = execution["id"] if execution else None
            records = observations_to_records(
                events,
                turn_id=turn_id,
                execution_id=execution_id,
                lease_generation=lease_row["generation"],
            )
            committed = ingest_runtime_events(
                uow,
                workspace_id=workspace_id,
                session_id=session_id,
                lease_id=lease_row["id"],
                runtime_epoch=runtime_epoch,
                events=records,
            )
            if active is not None:
                uow.sessions.update(workspace_id, session_id, {"active_lease_id": lease_row["id"]})
            for rec in records:
                project_observation(
                    uow,
                    session_id=session_id,
                    observation=rec["payload"],
                    turn_id=turn_id,
                    execution_id=execution_id,
                )
            # Advance the offset across ALL batch seqs — NOOP/skipped kinds
            # are deterministically skippable on replay, so the watermark
            # covers them even though they append no session event.
            batch_max = max((int(e.get("local_seq") or 0) for e in events), default=0)
            if batch_max:
                uow.ingestion_offsets.upsert_advance(
                    lease_id=lease_row["id"],
                    runtime_epoch=runtime_epoch,
                    session_id=session_id,
                    committed_local_seq=batch_max,
                )
            uow.commit()
            acked = batch_max or max((r["local_seq"] for r in committed), default=0)
        self._maybe_settle(lease_row)
        return acked

    def on_operation_result(self, lease_row: dict, frame: dict) -> None:
        op_id = str(frame.get("operation_id") or "")
        with self._pending_lock:
            self._pending_results[op_id] = {
                "state": frame.get("state"),
                "result": frame.get("result") or {},
                "final_local_seq": int(frame.get("final_local_seq") or 0),
                "lease": lease_row,
            }
        self._maybe_settle(lease_row)

    def _maybe_settle(self, lease_row: dict) -> None:
        with self._pending_lock:
            candidates = [
                (op, info)
                for op, info in self._pending_results.items()
                if info["lease"]["id"] == lease_row["id"]
            ]
        for op_id, info in candidates:
            watermark = self._committed_watermark(lease_row["id"])
            if info["final_local_seq"] <= watermark:
                self._pending_results.pop(op_id, None)
                self._settle_result(lease_row, op_id, info)

    def _committed_watermark(self, lease_id: str) -> int:
        with SqlUnitOfWork(self.db, actor={"kind": "runtime", "id": lease_id}) as uow:
            row = uow.rows.one(
                "SELECT COALESCE(MAX(committed_local_seq),0) AS w"
                " FROM runtime_ingestion_offsets WHERE executor_lease_id=%s",
                (lease_id,),
            )
            return int(row["w"]) if row else 0

    def _settle_result(self, lease_row: dict, operation_id: str, info: dict) -> None:
        """Authoritative terminal verdict for an Execution."""
        with SqlUnitOfWork(self.db, actor={"kind": "runtime", "id": lease_row["id"]}) as uow:
            execution = uow.executions.get_by_operation(operation_id)
            if execution is None:
                uow.commit()
                return
            workspace_id = execution["workspace_id"]
            session_id = execution["session_id"]
            turn_id = execution["turn_id"]
            turn = uow.turns.get_for_update(workspace_id, turn_id)
            outcome = (info.get("result") or {}).get("outcome") or {}
            state = str(info.get("state") or "unknown")
            exit_code = (info.get("result") or {}).get("exit_code")
            signal = (info.get("result") or {}).get("signal")
            final_seq = info.get("final_local_seq")

            if state == "succeeded":
                new_exec_state = ExecutionState.SUCCEEDED.value
                new_turn_state = TurnState.SUCCEEDED.value
                event_type = "turn.succeeded"
                reason = None
            elif state == "interrupted":
                new_exec_state = ExecutionState.CANCELLED.value
                new_turn_state = TurnState.CANCELLED.value
                event_type = "turn.cancelled"
                reason = "cancel_requested"
            elif state == "failed":
                new_exec_state = ExecutionState.FAILED.value
                new_turn_state = TurnState.FAILED.value
                event_type = "turn.failed"
                reason = outcome.get("reason") or "provider_error"
            else:
                new_exec_state = ExecutionState.UNKNOWN.value
                new_turn_state = TurnState.INTERRUPTED.value
                event_type = "turn.interrupted"
                reason = outcome.get("reason") or "outcome_unknown"
            uow.executions.update(
                workspace_id,
                execution["id"],
                {
                    "state": new_exec_state,
                    "outcome_evidence": {
                        "operation_state": state,
                        "outcome": outcome,
                        "exit_code": exit_code,
                        "signal": signal,
                    },
                    "final_watermark": final_seq,
                    "runtime_epoch": lease_row.get("runtime_epoch"),
                    "reason": reason,
                },
            )
            if turn is not None and turn["state"] not in (
                TurnState.SUCCEEDED.value,
                TurnState.FAILED.value,
                TurnState.CANCELLED.value,
                TurnState.INTERRUPTED.value,
            ):
                uow.turns.update(
                    workspace_id,
                    turn_id,
                    {
                        "state": new_turn_state,
                        "reason": reason,
                        "completed_at": _now(),
                        "outcome": {"operation_state": state, "outcome": outcome},
                    },
                    expected_version=turn["version"],
                )
                append_event(
                    uow,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    event_type=event_type,
                    turn_id=turn_id,
                    execution_id=execution["id"],
                    executor_lease_id=lease_row["id"],
                    lease_generation=lease_row["generation"],
                    payload={
                        "operation_id": operation_id,
                        "state": state,
                        "reason": reason,
                        "final_local_seq": final_seq,
                    },
                )
                output_message_id = (turn.get("resolved_settings") or {}).get("output_message_id")
                if output_message_id:
                    append_event(
                        uow,
                        workspace_id=workspace_id,
                        session_id=session_id,
                        event_type="message.completed",
                        turn_id=turn_id,
                        execution_id=execution["id"],
                        payload={"message_id": output_message_id},
                    )
            # Bump the logical Worktree generation — the turn's mediated
            # mutations completed (even a failed turn may have written).
            worktree = uow.worktrees.get_by_session(workspace_id, session_id)
            if worktree is not None:
                uow.worktrees.update(
                    workspace_id,
                    worktree["id"],
                    {
                        "generation": worktree["generation"] + 1,
                        "availability": "live",
                    },
                )
            if turn is not None:
                uow.sessions.update(
                    workspace_id,
                    session_id,
                    {"active_turn_id": None},
                )
            # Delegation finalization: if this session is a delegation child,
            # enqueue result publication so contract validation + waiter
            # wakeup survive worker restarts.
            delegation = uow.delegations.get_by_child_session(workspace_id, session_id)
            if delegation is not None and delegation["state"] in (
                "pending",
                "active",
                "waiting_result",
            ):
                from control.application.sessions import enqueue_job
                from control.domain.jobs import JobKind, TargetFamily

                enqueue_job(
                    uow,
                    workspace_id=workspace_id,
                    kind=JobKind.DELEGATION_PUBLISH_RESULT,
                    target_family=TargetFamily.DELEGATION,
                    target_id=delegation["id"],
                    dedupe_key=f"delegation.publish_result:{delegation['id']}:{turn_id}",
                    payload={
                        "delegation_id": delegation["id"],
                        "completing_turn_id": turn_id,
                    },
                )
            uow.commit()

    def _fail_execution(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        session_id: str,
        turn_id: str,
        execution_id: str,
        reason: str,
        error: dict,
    ) -> None:
        uow.executions.update(
            workspace_id,
            execution_id,
            {"state": ExecutionState.FAILED.value, "reason": reason, "outcome_evidence": error},
        )
        turn = uow.turns.get_for_update(workspace_id, turn_id)
        if turn is not None:
            uow.turns.update(
                workspace_id,
                turn_id,
                {
                    "state": TurnState.FAILED.value,
                    "reason": reason,
                    "completed_at": _now(),
                },
                expected_version=turn["version"],
            )
        append_event(
            uow,
            workspace_id=workspace_id,
            session_id=session_id,
            event_type="turn.failed",
            turn_id=turn_id,
            execution_id=execution_id,
            payload={"reason": reason, "error": error},
        )
        uow.sessions.update(workspace_id, session_id, {"active_turn_id": None})

    # ------------------------------------------------------------------
    # loss / reconcile
    # ------------------------------------------------------------------
    def handle_detach(self, lease_row: dict, runtime_epoch: str) -> None:
        """Transport lost — evidence-only lease transition (not a verdict on
        live compute; unverified compute is quarantined and terminated by
        the owner)."""
        with SqlUnitOfWork(self.db, actor={"kind": "system"}) as uow:
            lease = uow.leases.get_for_update(lease_row["workspace_id"], lease_row["id"])
            if lease is None or lease["state"] in (
                LeaseState.RELEASED.value,
                LeaseState.LOST.value,
            ):
                uow.commit()
                return
            self._mark_lease_lost(uow, lease)
            uow.commit()

    def _mark_lease_lost(
        self, uow: SqlUnitOfWork, lease: dict, *, spawn_failed: bool = False
    ) -> None:
        """Lease declared LOST.

        ``spawn_failed``: the daemon never enrolled — no operation was ever
        submitted to this compute, so bound executions *failed* (known) and
        their turns requeue; a mid-flight loss instead marks executions
        ``unknown`` and interrupts turns (outcome unknowable)."""
        workspace_id = lease["workspace_id"]
        session_id = lease["session_id"]
        uow.leases.update(
            workspace_id,
            lease["id"],
            {"state": LeaseState.LOST.value, "observed_at": _now()},
        )
        append_event(
            uow,
            workspace_id=workspace_id,
            session_id=session_id,
            event_type="executor.unavailable",
            executor_lease_id=lease["id"],
            lease_generation=lease["generation"],
            payload={
                "lease_id": lease["id"],
                "state": "lost",
                "spawn_failed": spawn_failed,
            },
        )
        # Nonterminal executions on this lease. Conditional
        # update — a just-settled verdict must not be overwritten.
        rows = uow.rows.all(
            "SELECT id, turn_id FROM executions WHERE executor_lease_id=%s"
            " AND state IN ('preparing','started','stop_requested')",
            (lease["id"],),
        )
        if spawn_failed:
            for row in rows:
                uow.conn.execute(
                    "UPDATE executions SET state=%s, reason=%s, updated_at=now()"
                    " WHERE id=%s AND state IN ('preparing','started','stop_requested')",
                    (ExecutionState.FAILED.value, "lease_spawn_failed", row["id"]),
                )
                # The turn never ran: send it back to QUEUED so the retried
                # dispatch allocates a fresh lease. Conditional — an already
                # settled turn is never reopened.
                uow.conn.execute(
                    "UPDATE turns SET state=%s, version=version+1, updated_at=now()"
                    " WHERE id=%s AND workspace_id=%s AND state='preparing'",
                    (TurnState.QUEUED.value, row["turn_id"], workspace_id),
                )
                append_event(
                    uow,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    event_type="turn.requeued",
                    turn_id=row["turn_id"],
                    execution_id=row["id"],
                    executor_lease_id=lease["id"],
                    payload={"reason": "lease_spawn_failed"},
                )
            uow.sessions.update(workspace_id, session_id, {"active_lease_id": None})
            return
        for row in rows:
            uow.conn.execute(
                "UPDATE executions SET state=%s, reason=%s, updated_at=now()"
                " WHERE id=%s AND state IN ('preparing','started','stop_requested')",
                (ExecutionState.UNKNOWN.value, "lease_lost", row["id"]),
            )
            turn = uow.turns.get_for_update(workspace_id, row["turn_id"])
            if turn is not None and turn["state"] not in (
                TurnState.SUCCEEDED.value,
                TurnState.FAILED.value,
                TurnState.CANCELLED.value,
                TurnState.INTERRUPTED.value,
            ):
                uow.turns.update(
                    workspace_id,
                    row["turn_id"],
                    {
                        "state": TurnState.INTERRUPTED.value,
                        "reason": "outcome_unknown",
                        "completed_at": _now(),
                    },
                    expected_version=turn["version"],
                )
                append_event(
                    uow,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    event_type="turn.interrupted",
                    turn_id=row["turn_id"],
                    execution_id=row["id"],
                    executor_lease_id=lease["id"],
                    payload={"reason": "lease_lost"},
                )
        uow.sessions.update(workspace_id, session_id, {"active_lease_id": None})

    def reconcile_expired_leases(self, uow: SqlUnitOfWork, before: Any) -> int:
        """Reconcile handler — expiry is lease policy; terminations are
        best-effort backend calls."""
        rows = uow.leases.list_expired(before)
        n = 0
        for lease in rows:
            self._mark_lease_lost(uow, lease)
            self._terminate_with_owner(uow, lease)
            self.pool.revoke(lease["id"])
            n += 1
        return n

    def _terminate_with_owner(self, uow: SqlUnitOfWork, lease: dict) -> None:
        """Best-effort teardown under the OWNER's compute credential — never
        the ambient profile (teardown authority must live with the user who
        provisioned the sandbox)."""
        backend = self.backends.get(lease["backend"])
        if backend is None:
            return
        from control.executors.port import ExecutorHandle

        meta = lease.get("handle") or {}
        connection = {}
        if lease["backend"] == "modal":
            session = uow.sessions.get(lease["workspace_id"], lease["session_id"])
            if session is not None:
                try:
                    connection = self.compute_resolver(
                        session,
                        workspace_id=lease["workspace_id"],
                        lease_id=lease["id"],
                    )
                except Exception:
                    connection = {}
        try:
            backend.terminate(
                ExecutorHandle(
                    backend=lease["backend"],
                    handle_id=str(meta.get("handle_id") or lease["id"]),
                    metadata=meta,
                ),
                lease["allocation_operation_id"] or lease["id"],
                connection=connection,
            )
        except Exception:
            pass

    def cancel_turn(self, uow: SqlUnitOfWork, *, workspace_id: str, turn_id: str) -> dict:
        """Cancel-intent: persist cancel + send turn.cancel to runtime."""
        turn = uow.turns.get_for_update(workspace_id, turn_id)
        if turn is None:
            raise NotFound("turn")
        if turn["state"] in (
            TurnState.SUCCEEDED.value,
            TurnState.FAILED.value,
            TurnState.CANCELLED.value,
            TurnState.INTERRUPTED.value,
        ):
            return {"turn_id": turn_id, "already": turn["state"]}
        require_turn_transition(TurnState(turn["state"]), TurnState.CANCELLING)
        uow.turns.update(
            workspace_id,
            turn_id,
            {"state": TurnState.CANCELLING.value, "cancel_intent": {"at": str(_now())}},
            expected_version=turn["version"],
        )
        session = uow.sessions.get_for_update(workspace_id, turn["session_id"])
        append_event(
            uow,
            workspace_id=workspace_id,
            session_id=turn["session_id"],
            event_type="turn.cancel_requested",
            turn_id=turn_id,
            payload={},
        )
        execution = uow.executions.nonterminal_by_turn(workspace_id, turn_id)
        if execution is not None and execution["executor_lease_id"]:
            lease = uow.leases.get(workspace_id, execution["executor_lease_id"])
            if lease is not None:
                uow.executions.update(
                    workspace_id,
                    execution["id"],
                    {"state": ExecutionState.STOP_REQUESTED.value},
                )
                uow.commit()
                try:
                    self.pool.submit(
                        lease["id"],
                        {
                            "operation_id": ids.new_id("effect"),
                            "operation_kind": OperationKind.TURN_CANCEL.value,
                            "session_id": session["id"] if session else turn["session_id"],
                            "lease_id": lease["id"],
                            "lease_generation": lease["generation"],
                            "payload": {"operation_id": execution["operation_id"]},
                        },
                        timeout=10,
                    )
                except Exception:
                    pass
                return {"turn_id": turn_id, "cancel_signalled": True}
        return {"turn_id": turn_id, "cancel_signalled": False}


def make_dispatch_handler(service: ExecutionService):
    """The turn.dispatch job handler."""

    def handler(job: dict, ctx) -> dict:
        payload = job.get("payload") or {}
        turn_id = payload.get("turn_id") or job.get("target_id")
        return service.dispatch_turn(ctx.uow, workspace_id=job["workspace_id"], turn_id=turn_id)

    return handler
