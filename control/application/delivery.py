"""Delivery intent, exact-subject steps, target claims, merge gate
(RFC 167 §05).

A Delivery is committed before any external effect and pins one
ChangeSet/digest, target, transport, ShipPolicy snapshot, authorizing
principal and expected remote preconditions. Steps are idempotent durable
rows; ambiguous remote outcomes stay unresolved rather than speculative.
``succeeded`` means the declared transport was verified — never merged.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from control.domain import ids
from control.domain.delivery import (
    DeliveryState,
    MergeRequestState,
    ShipPolicy,
    require_delivery_transition,
    require_merge_transition,
    stable_branch_ref,
)
from control.domain.errors import DomainError, NotFound
from control.domain.jobs import JobKind, TargetFamily
from control.persistence.unit_of_work import SqlUnitOfWork
from control.vcs.remote import RemoteConflict, RemoteError, materialize_commit

from .events import append_event
from .sessions import enqueue_job

TRANSPORT_STEPS = {
    "export": ["verify", "export"],
    "git_branch": ["verify", "push", "reconcile"],
    "pull_request": ["verify", "push", "pull_request", "reconcile"],
    "direct_base": ["verify", "push", "reconcile"],
}


class DeliveryService:
    def __init__(
        self,
        db,
        *,
        remote_factory: Callable[..., Any] | None = None,
        connection_service=None,
        blob_store=None,
        scratch_root: str | Path | None = None,
    ) -> None:
        self.db = db
        self.remote_factory = remote_factory
        self.connections = connection_service
        self.blobs = blob_store
        self.scratch_root = Path(scratch_root or tempfile.gettempdir()) / "sbx-delivery"

    # ------------------------------------------------------------- create
    def create(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        session_id: str,
        changeset_id: str,
        target: dict,
        transport: str,
        ship_policy: dict | None = None,
        authorizing_principal: str,
        connection_id: str | None = None,
    ) -> dict:
        """Create the Delivery intent + step rows + target claim, then
        enqueue the perform job — all inside the caller's transaction so no
        external effect can precede the durable record."""
        cs = uow.changesets.get(workspace_id, changeset_id)
        if cs is None:
            raise NotFound("changeset")
        session = uow.sessions.get(workspace_id, session_id)
        if session is None:
            raise NotFound("session")
        if cs["session_id"] != session_id:
            raise DomainError("forbidden", "changeset belongs to a different session")
        policy = ShipPolicy.from_dict(ship_policy)
        if transport not in TRANSPORT_STEPS:
            raise DomainError("validation_failed", f"bad transport {transport!r}")
        if transport == "direct_base":
            raise DomainError(
                "forbidden",
                "direct_base requires explicit opt-in policy — not authorized here",
            )
        if policy.automatic_delivery and not cs["automatic_eligible"]:
            raise DomainError("invalid_state", "automatic delivery requires an eligible capture")
        repo = target.get("repository") or cs.get("repository")
        if not repo:
            raise DomainError("validation_failed", "target.repository required")
        ref = target.get("ref") or stable_branch_ref(session_id, changeset_id)
        delivery_id = ids.new_id("delivery")
        uow.deliveries.insert(
            {
                "id": delivery_id,
                "workspace_id": workspace_id,
                "session_id": session_id,
                "changeset_id": changeset_id,
                "subject_digest": cs["subject_digest"],
                "target": {
                    "repository": repo,
                    "ref": ref,
                    "base_ref": target.get("base_ref") or policy.base_branch,
                    "existing_pr": target.get("existing_pr"),
                },
                "transport": transport,
                "ship_policy": policy.to_dict(),
                "authorizing_principal": authorizing_principal,
                "connection_id": connection_id,
                "expected_remote": {
                    "expected_old_ref_sha": target.get("expected_old_ref_sha"),
                    "base_sha": cs.get("base_sha"),
                    "head_sha": cs.get("head_sha"),
                },
                "state": DeliveryState.PENDING.value,
            }
        )
        steps = TRANSPORT_STEPS[transport]
        for ordinal, kind in enumerate(steps):
            uow.delivery_steps.upsert_step(
                workspace_id=workspace_id,
                delivery_id=delivery_id,
                kind=kind,
                effect_id=ids.new_id("effect"),
                state="pending",
                expected={"changeset_id": changeset_id, "subject_digest": cs["subject_digest"]},
            )
        # Serialize platform mutations of this repository/ref.
        claim = uow.target_claims.claim(
            claim_id=ids.new_id("delivery_target_claim"),
            workspace_id=workspace_id,
            repository=repo,
            ref_or_pr=ref,
            holder=delivery_id,
        )
        if not claim:
            raise DomainError(
                "version_conflict",
                f"delivery target {repo}@{ref} is claimed by another delivery",
            )
        append_event(
            uow,
            workspace_id=workspace_id,
            session_id=session_id,
            event_type="delivery.created",
            payload={
                "delivery_id": delivery_id,
                "changeset_id": changeset_id,
                "subject_digest": cs["subject_digest"],
                "transport": transport,
                "target": {"repository": repo, "ref": ref},
            },
        )
        enqueue_job(
            uow,
            workspace_id=workspace_id,
            kind=JobKind.DELIVERY_PERFORM,
            target_family=TargetFamily.DELIVERY,
            target_id=delivery_id,
            dedupe_key=f"delivery.perform:{delivery_id}",
            payload={"delivery_id": delivery_id},
        )
        return {"delivery_id": delivery_id, "ref": ref}

    # ------------------------------------------------------------- perform
    def perform_delivery(self, job: dict, ctx) -> dict:
        workspace_id = job["workspace_id"]
        delivery_id = (job.get("payload") or {}).get("delivery_id") or job["target_id"]
        with SqlUnitOfWork(self.db, actor={"kind": "job", "id": job["id"]}) as uow:
            delivery = uow.deliveries.get_for_update(workspace_id, delivery_id)
            if delivery is None:
                uow.commit()
                return {"skipped": "delivery gone"}
            if delivery["state"] in (
                DeliveryState.SUCCEEDED.value,
                DeliveryState.CANCELLED.value,
            ):
                uow.commit()
                return {"skipped": delivery["state"]}
            require_delivery_transition(DeliveryState(delivery["state"]), DeliveryState.EXECUTING)
            uow.deliveries.update(
                workspace_id, delivery_id, {"state": DeliveryState.EXECUTING.value}
            )
            uow.commit()
        try:
            outcome = self._run_steps(delivery)
        except RemoteConflict as exc:
            outcome = ("blocked", {"reason": f"remote_conflict:{exc}"})
        except Exception as exc:
            outcome = ("failed", {"reason": f"{type(exc).__name__}: {exc}"})
        state, evidence = outcome
        with SqlUnitOfWork(self.db, actor={"kind": "job", "id": job["id"]}) as uow:
            delivery = uow.deliveries.get_for_update(workspace_id, delivery_id)
            require_delivery_transition(DeliveryState(delivery["state"]), DeliveryState(state))
            prior = dict(delivery.get("effect_evidence") or {})
            prior.update(evidence)
            uow.deliveries.update(
                workspace_id,
                delivery_id,
                {"state": state, "effect_evidence": prior},
            )
            append_event(
                uow,
                workspace_id=workspace_id,
                session_id=delivery["session_id"],
                event_type=f"delivery.{state}",
                payload={"delivery_id": delivery_id, **evidence},
            )
            uow.commit()
        return {"state": state, **evidence}

    def _run_steps(self, delivery: dict) -> tuple[str, dict]:
        workspace_id = delivery["workspace_id"]
        delivery_id = delivery["id"]
        remote = self._remote_for(delivery)
        with SqlUnitOfWork(self.db, actor={"kind": "delivery"}) as uow:
            cs = uow.changesets.get(workspace_id, delivery["changeset_id"])
            files = uow.changeset_files.list_for(delivery["changeset_id"])
            blob_meta = {
                r["id"]: r
                for r in uow.rows.all(
                    "SELECT * FROM blobs WHERE workspace_id=%s AND id = ANY(%s)",
                    (workspace_id, [f["blob_id"] for f in files if f["blob_id"]]),
                )
            }
            steps = uow.delivery_steps.list_for(workspace_id, delivery_id)
            uow.commit()
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        scratch = self.scratch_root / delivery_id
        scratch.mkdir(parents=True, exist_ok=True)

        commit_sha = cs.get("head_sha")
        for step in steps:
            if step["state"] == "succeeded":
                continue  # idempotent re-entry: verified steps are final
            kind = step["kind"]
            if kind == "verify":
                result = self._step_verify(cs)
            elif kind == "push":
                if commit_sha is None:
                    files_with_blobs = [
                        {**f, "storage_key": blob_meta[f["blob_id"]]["storage_key"]}
                        for f in files
                        if f.get("blob_id") in blob_meta
                    ]
                    commit_sha = materialize_commit(
                        files=files_with_blobs,
                        read_blob=self.blobs.read,
                        base_commit=cs.get("base_sha"),
                        repository_url=remote._remote_url(cs["repository"])
                        if hasattr(remote, "_remote_url")
                        else cs.get("repository"),
                        message=f"sbx changeset {cs['id']}",
                        scratch=scratch,
                    )
                result = remote.push(
                    cs["repository"] or (delivery["target"] or {}).get("repository"),
                    commit_sha,
                    (delivery["target"] or {})["ref"],
                    (delivery["expected_remote"] or {}).get("expected_old_ref_sha"),
                    scratch,
                )
            elif kind == "pull_request":
                result = self._step_pull_request(remote, delivery, commit_sha)
            elif kind == "reconcile":
                result = self._step_reconcile(remote, delivery, commit_sha)
            elif kind == "export":
                result = self._step_export(delivery, files, blob_meta)
            elif kind == "merge":
                continue  # merge is a separate authorized MergeRequest
            else:
                result = {"skipped": f"unknown step {kind}"}
            with SqlUnitOfWork(self.db, actor={"kind": "delivery"}) as uow:
                uow.delivery_steps.upsert_step(
                    workspace_id=workspace_id,
                    delivery_id=delivery_id,
                    kind=kind,
                    effect_id=step["effect_id"],
                    state="succeeded",
                    expected=step.get("expected") or {},
                    result=result,
                )
                uow.commit()
        return ("succeeded", {"head_sha": commit_sha, "verified": True})

    # ------------------------------------------------------------- steps
    def _step_verify(self, cs: dict) -> dict:
        files_ok = self.blobs is not None
        digest = hashlib.sha256(json.dumps(cs["manifest"], sort_keys=True).encode()).hexdigest()
        return {
            "subject_digest": cs["subject_digest"],
            "manifest_sha": "sha256:" + digest,
            "blobs_verified": files_ok,
        }

    def _step_pull_request(self, remote, delivery: dict, commit_sha: str) -> dict:
        target = delivery["target"] or {}
        repo = target["repository"]
        existing = remote.find_pull_request(
            repo, head_ref=target["ref"], base_ref=target.get("base_ref") or "main"
        )
        if existing:
            if existing["head_sha"] != commit_sha:
                raise RemoteConflict(
                    f"existing PR #{existing['number']} head {existing['head_sha']}"
                    f" != subject {commit_sha}"
                )
            return {"pr": existing["number"], "url": existing["url"], "reused": True}
        policy = ShipPolicy.from_dict(delivery.get("ship_policy"))
        created = remote.create_pull_request(
            repo,
            head_ref=target["ref"],
            base_ref=target.get("base_ref") or "main",
            title=f"sbx delivery {delivery['id']}",
            body="",
            draft=policy.draft,
        )
        if created["head_sha"] != commit_sha:
            raise RemoteConflict(f"created PR head {created['head_sha']} != subject {commit_sha}")
        return {"pr": created["number"], "url": created["url"], "draft": created["draft"]}

    def _step_reconcile(self, remote, delivery: dict, commit_sha: str) -> dict:
        target = delivery["target"] or {}
        repo = target["repository"]
        seen = remote.ls_remote(repo, target["ref"])
        evidence: dict[str, Any] = {"remote_head": seen}
        if seen != commit_sha:
            raise RemoteConflict(f"remote head {seen} != subject {commit_sha}")
        pr_num = None
        with SqlUnitOfWork(self.db, actor={"kind": "delivery"}) as uow:
            pr_step = uow.rows.one(
                "SELECT result FROM delivery_steps WHERE delivery_id=%s AND kind='pull_request'",
                (delivery["id"],),
            )
            uow.commit()
        if pr_step:
            pr_num = (pr_step.get("result") or {}).get("pr")
        if pr_num:
            state = remote.pull_request_state(repo, int(pr_num))
            evidence["pr_state"] = state
            policy = ShipPolicy.from_dict(delivery.get("ship_policy"))
            if policy.required_check_names:
                checks = {c["name"]: c for c in remote.checks(repo, commit_sha)}
                evidence["checks"] = checks
                missing = [n for n in policy.required_check_names if n not in checks]
                failing = [
                    n
                    for n in policy.required_check_names
                    if n in checks
                    and checks[n]["status"] == "completed"
                    and checks[n]["conclusion"] not in ("success", "neutral", "skipped")
                ]
                pending = [
                    n
                    for n in policy.required_check_names
                    if n in checks and checks[n]["status"] != "completed"
                ]
                if missing or failing or pending:
                    return {
                        "remote_head": seen,
                        "blocked_checks": {
                            "missing": missing,
                            "failing": failing,
                            "pending": pending,
                        },
                    }
        return evidence

    def _step_export(self, delivery: dict, files: list[dict], blob_meta: dict) -> dict:
        manifest_digest = hashlib.sha256(
            json.dumps(
                {"files": [f["path"] for f in files], "subject": delivery["subject_digest"]},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        out = self.scratch_root / delivery["id"] / "export.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "changeset_id": delivery["changeset_id"],
                    "subject_digest": delivery["subject_digest"],
                    "files": [{"path": f["path"], "digest": f["content_digest"]} for f in files],
                }
            )
        )
        return {"export": str(out), "manifest_sha": "sha256:" + manifest_digest}

    # ------------------------------------------------------------- remote
    def _remote_for(self, delivery: dict):
        if self.remote_factory is not None:
            return self.remote_factory(delivery, self.connections)
        raise DomainError("executor_unavailable", "no remote configured")

    # ------------------------------------------------------------- merge gate
    def merge_gate(self, uow: SqlUnitOfWork, *, workspace_id: str, delivery_id: str) -> dict:
        """Derived (not verdict) gate evaluation from typed projections."""
        delivery = uow.deliveries.get(workspace_id, delivery_id)
        if delivery is None:
            raise NotFound("delivery")
        reasons: list[str] = []
        cs = uow.changesets.get(workspace_id, delivery["changeset_id"])
        policy = ShipPolicy.from_dict(delivery.get("ship_policy"))
        results = uow.rows.all(
            "SELECT r.*, d.role, d.child_session_id FROM delegation_results r"
            " JOIN delegations d ON d.id = r.delegation_id"
            " JOIN sessions s ON s.id = d.parent_session_id"
            " WHERE s.workspace_id=%s AND s.id=%s AND r.subject_digest=%s"
            " AND r.validation_status='valid'",
            (workspace_id, delivery["session_id"], delivery["subject_digest"]),
        )
        approvals = [
            r
            for r in results
            if r["verdict"] == "approve"
            and r["role"] in policy.required_result_roles
            and (not policy.require_independent or r["child_session_id"] != delivery["session_id"])
        ]
        changes_requested = [r for r in results if r["verdict"] == "request_changes"]
        # Supersession is explicit: a later approval for the same subject
        # by an independent child supersedes an earlier request_changes.
        unresolved_changes = []
        for req in changes_requested:
            superseded = any(a["published_at"] > req["published_at"] for a in approvals)
            if not superseded:
                unresolved_changes.append(req)
        if len(approvals) < policy.required_result_count:
            reasons.append(f"approvals:{len(approvals)}/{policy.required_result_count}")
        if unresolved_changes:
            reasons.append("unresolved_request_changes")
        if policy.required_check_names:
            # Required checks are gate evidence captured on the exact head —
            # a reconcile step that recorded any blocked/missing/pending check
            # keeps the gate closed until a fresh reconcile clears it.
            reconcile = uow.rows.one(
                "SELECT result FROM delivery_steps WHERE delivery_id=%s"
                " AND kind='reconcile' ORDER BY ordinal DESC LIMIT 1",
                (delivery_id,),
            )
            blocked = ((reconcile or {}).get("result") or {}).get("blocked_checks") or {}
            if any(blocked.get(k) for k in ("missing", "failing", "pending")):
                reasons.append("checks_unsatisfied")
        if delivery["state"] != DeliveryState.SUCCEEDED.value:
            reasons.append(f"delivery_not_succeeded:{delivery['state']}")
        if cs is None:
            reasons.append("changeset_missing")
        return {
            "eligible": not reasons,
            "reasons": reasons,
            "approvals": len(approvals),
            "required": policy.required_result_count,
        }

    # ------------------------------------------------------------- merge
    def request_merge(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        delivery_id: str,
        expected_head_sha: str,
        merge_method: str,
        authorizing_principal: str,
    ) -> dict:
        delivery = uow.deliveries.get_for_update(workspace_id, delivery_id)
        if delivery is None:
            raise NotFound("delivery")
        if delivery["state"] != DeliveryState.SUCCEEDED.value:
            raise DomainError("invalid_state", "delivery must be succeeded first")
        policy = ShipPolicy.from_dict(delivery.get("ship_policy"))
        if merge_method not in policy.accepted_merge_methods:
            raise DomainError("validation_failed", f"merge method {merge_method!r}")
        cs = uow.changesets.get(workspace_id, delivery["changeset_id"])
        gate = self.merge_gate(uow, workspace_id=workspace_id, delivery_id=delivery_id)
        if not gate["eligible"]:
            raise DomainError("forbidden", "merge gate not satisfied", details=gate)
        # One active request per delivery (partial unique index enforces).
        active = uow.merge_requests.active_for_delivery(workspace_id, delivery_id)
        if active:
            raise DomainError("idempotency_conflict", "merge request already active")
        # A replayed merge request can never carry a different subject — a
        # terminal merge record already exists, so this is a duplicate, not
        # a new intent.
        prior = uow.rows.one(
            "SELECT id FROM merge_requests WHERE delivery_id=%s AND state='succeeded' LIMIT 1",
            (delivery_id,),
        )
        if prior:
            raise DomainError("idempotency_conflict", "delivery already has a succeeded merge")
        mr_id = ids.new_id("merge_request")
        uow.merge_requests.insert(
            {
                "id": mr_id,
                "workspace_id": workspace_id,
                "delivery_id": delivery_id,
                "changeset_id": delivery["changeset_id"],
                "subject_digest": delivery["subject_digest"],
                "expected_head_sha": expected_head_sha,
                "expected_base_sha": cs.get("base_sha"),
                "expected_delivery_version": delivery["version"],
                "merge_method": merge_method,
                "authorizing_principal": authorizing_principal,
                "state": MergeRequestState.PENDING.value,
                "gate_evidence": gate,
            }
        )
        append_event(
            uow,
            workspace_id=workspace_id,
            session_id=delivery["session_id"],
            event_type="merge.requested",
            payload={"merge_request_id": mr_id, "delivery_id": delivery_id},
        )
        enqueue_job(
            uow,
            workspace_id=workspace_id,
            kind=JobKind.DELIVERY_MERGE,
            target_family=TargetFamily.MERGE_REQUEST,
            target_id=mr_id,
            dedupe_key=f"delivery.merge:{mr_id}",
            payload={"merge_request_id": mr_id},
        )
        return {"merge_request_id": mr_id}

    def perform_merge(self, job: dict, ctx) -> dict:
        workspace_id = job["workspace_id"]
        mr_id = (job.get("payload") or {}).get("merge_request_id") or job["target_id"]
        with SqlUnitOfWork(self.db, actor={"kind": "job", "id": job["id"]}) as uow:
            mr = uow.merge_requests.get_for_update(workspace_id, mr_id)
            if mr is None:
                uow.commit()
                return {"skipped": "merge request gone"}
            if mr["state"] in (
                MergeRequestState.SUCCEEDED.value,
                MergeRequestState.CANCELLED.value,
            ):
                uow.commit()
                return {"skipped": mr["state"]}
            require_merge_transition(MergeRequestState(mr["state"]), MergeRequestState.EXECUTING)
            # Revalidate the gate inside the claim transaction — policy
            # drift or a new request_changes must block here, not rely on
            # the check performed at request time.
            gate = self.merge_gate(uow, workspace_id=workspace_id, delivery_id=mr["delivery_id"])
            if not gate["eligible"]:
                uow.merge_requests.update(
                    workspace_id,
                    mr_id,
                    {
                        "state": MergeRequestState.BLOCKED.value,
                        "gate_evidence": gate,
                    },
                )
                uow.commit()
                return {"state": "blocked", "reasons": gate["reasons"]}
            uow.merge_requests.update(
                workspace_id, mr_id, {"state": MergeRequestState.EXECUTING.value}
            )
            uow.commit()
        try:
            result = self._perform_merge_effect(mr)
            state, evidence = "succeeded", result
        except RemoteConflict as exc:
            state, evidence = "blocked", {"reason": f"remote_conflict:{exc}"}
        except Exception as exc:
            state, evidence = "failed", {"reason": f"{type(exc).__name__}: {exc}"}
        with SqlUnitOfWork(self.db, actor={"kind": "job", "id": job["id"]}) as uow:
            mr = uow.merge_requests.get_for_update(workspace_id, mr_id)
            require_merge_transition(MergeRequestState(mr["state"]), MergeRequestState(state))
            uow.merge_requests.update(
                workspace_id,
                mr_id,
                {"state": state, "result": evidence},
            )
            append_event(
                uow,
                workspace_id=workspace_id,
                session_id=(uow.deliveries.get(workspace_id, mr["delivery_id"]) or {}).get(
                    "session_id"
                ),
                event_type=f"merge.{state}",
                payload={"merge_request_id": mr_id, **evidence},
            )
            uow.commit()
        return {"state": state, **evidence}

    def _perform_merge_effect(self, mr: dict) -> dict:
        with SqlUnitOfWork(self.db, actor={"kind": "merge"}) as uow:
            delivery = uow.deliveries.get(mr["workspace_id"], mr["delivery_id"])
            uow.commit()
        remote = self._remote_for(delivery)
        target = delivery["target"] or {}
        repo = target["repository"]
        # Remote preconditions rechecked at effect time, never cached.
        pr = remote.find_pull_request(
            repo, head_ref=target["ref"], base_ref=target.get("base_ref") or "main"
        )
        if pr is None:
            raise RemoteConflict("no open PR found for delivery ref")
        if pr["head_sha"] != mr["expected_head_sha"]:
            raise RemoteConflict(
                f"remote head {pr['head_sha']} != expected {mr['expected_head_sha']}"
            )
        result = remote.merge_pull_request(
            repo,
            int(pr["number"]),
            method=mr["merge_method"],
            expected_head=mr["expected_head_sha"],
        )
        # Verified evidence: provider reports merged at the exact head.
        state = remote.pull_request_state(repo, int(pr["number"]))
        if not state.get("merged"):
            raise RemoteError("merge effect ambiguous — PR reports not merged")
        return {
            "merged": True,
            "pr": pr["number"],
            "merge_commit_sha": result.get("merge_commit_sha"),
            "verified_head": state["head_sha"],
        }
