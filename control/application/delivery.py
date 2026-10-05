"""Delivery application: exact-subject Git effects, reconcile and gated merge (RFC 05)."""

from __future__ import annotations

from typing import Any

from control.application import access
from control.domain.delivery import evaluate_merge_gate, target_ref
from control.domain.errors import DomainError
from control.domain.identity import Principal
from control.domain.ids import new_id
from control.jobs.model import Outcome, Retry, Succeeded


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


class Deliveries:
    def __init__(self, tx: Any, broker: Any, git: Any, host: Any, blobs: Any) -> None:
        self.tx = tx
        self.broker = broker
        self.git = git
        self.host = host
        self.blobs = blobs

    # ------------------------------------------------------------------ views
    def view(self, uow: Any, d: dict[str, Any]) -> dict[str, Any]:
        cs = uow.get("changesets", d["changeset_id"])
        steps = uow.find("delivery_steps", {"delivery_id": d["id"]}, order="created_at")
        merges = uow.find("merge_requests", {"delivery_id": d["id"]}, order="created_at DESC")
        results = self._results(uow, d)
        gate = evaluate_merge_gate(
            changeset=cs,
            delivery=d,
            merge_request=None,
            results=results,
            policy=d["policy"],
            now=uow.now(),
        )
        return {
            "id": d["id"],
            "session_id": d["session_id"],
            "changeset_id": d["changeset_id"],
            "subject_digest": d["subject_digest"],
            "transport": d["transport"],
            "repository": d["repository"],
            "target_ref": d["target_ref"],
            "base_branch": d["base_branch"],
            "draft": d["draft"],
            "state": d["state"],
            "state_reason": d["state_reason"],
            "authorization": d["authorization_kind"],
            "commit_sha": d["commit_sha"],
            "pull_request": {
                "number": d["pr_number"],
                "url": d["pr_url"],
                "state": d["pr_state"],
                "draft": d["pr_draft"],
            }
            if d["pr_number"]
            else None,
            "remote": {
                "head_sha": d["remote_head_sha"],
                "checks_state": d["checks_state"],
                "checks": d["checks"],
                "observed_at": _iso(d["observed_at"]),
            },
            "steps": [
                {
                    "kind": s["kind"],
                    "outcome": s["outcome"],
                    "evidence": s["evidence"],
                    "created_at": _iso(s["created_at"]),
                }
                for s in steps
            ],
            "merge_requests": [
                {
                    "id": m["id"],
                    "state": m["state"],
                    "gate": m["gate"],
                    "merge_sha": m["merge_sha"],
                    "error": m["error"],
                }
                for m in merges
            ],
            "merge_eligibility": gate,
            "version": d["version"],
            "created_at": _iso(d["created_at"]),
        }

    def _results(self, uow: Any, d: dict[str, Any]) -> list[dict[str, Any]]:
        children = [
            x["id"] for x in uow.find("delegations", {"parent_session_id": d["session_id"]})
        ]
        return uow.find("delegation_results", {"delegation_id": children}) if children else []

    def get(self, principal: Principal, delivery_id: str) -> dict[str, Any]:
        return self.tx.read(
            lambda uow: self.view(
                uow, access.owned(uow, principal, "deliveries", delivery_id, what="delivery")
            )
        )

    def list(self, principal: Principal, changeset_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            access.owned(uow, principal, "changesets", changeset_id, what="changeset")
            return {
                "items": [
                    self.view(uow, d)
                    for d in uow.find(
                        "deliveries", {"changeset_id": changeset_id}, order="created_at DESC"
                    )
                ]
            }

        return self.tx.read(fn)

    # --------------------------------------------------------------- commands
    def request(
        self,
        principal: Principal,
        changeset_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            cs = access.owned(uow, principal, "changesets", changeset_id, what="changeset")
            replay = uow.dedupe_begin(
                principal_id=principal.user_id,
                workspace_id=cs["workspace_id"],
                command_kind=f"deliveries.create:{changeset_id}",
                key=idempotency_key,
                request=body,
            )
            if replay is not None:
                return replay
            d = self.create_in(uow, principal.user_id, cs, body, authorization="explicit")
            response = {
                "delivery": self.view(uow, d),
                "event_watermark": uow.watermark(cs["session_id"]),
            }
            uow.dedupe_finish(
                principal_id=principal.user_id,
                workspace_id=cs["workspace_id"],
                command_kind=f"deliveries.create:{changeset_id}",
                key=idempotency_key,
                response=response,
            )
            return response

        return self.tx.run(fn)

    def create_in(
        self, uow: Any, actor: str, cs: dict[str, Any], body: dict[str, Any], *, authorization: str
    ) -> dict[str, Any]:
        if cs["state"] != "ready":
            raise DomainError("stale_subject", "only sealed ChangeSets can be delivered")
        if authorization == "automatic" and not cs["automatic_eligible"]:
            raise DomainError("gate_blocked", "ChangeSet is not eligible for automatic delivery")
        session = uow.get("sessions", cs["session_id"], lock=True)
        spec = session["effective_spec"]
        repo = spec.get("repository")
        if not repo:
            raise DomainError(
                "unsupported_capability", "projectless ChangeSets support export only (not enabled)"
            )
        if cs["baseline_tree"] and cs["base_sha"] is None:
            raise DomainError("stale_subject", "ChangeSet has no remote baseline")
        policy = spec.get("ship_policy") or {}
        connection_id = body.get("connection_id") or session["source_connection_id"]
        con = uow.get("connections", connection_id or "", workspace_ids=[cs["workspace_id"]])
        if con is None or con["kind"] != "github":
            raise DomainError(
                "connection_required",
                "a GitHub Connection is required for Git delivery",
                details={"kind": "github"},
            )
        if con["config_state"] != "configured":
            raise DomainError("connection_revoked", "GitHub Connection is revoked")
        transport = body.get("transport") or policy.get("transport") or "pull_request"
        if transport not in ("git_branch", "pull_request"):
            raise DomainError("unsupported_capability", f"transport {transport} is not enabled")
        d = uow.insert(
            "deliveries",
            {
                "id": new_id("delivery"),
                "workspace_id": cs["workspace_id"],
                "session_id": cs["session_id"],
                "changeset_id": cs["id"],
                "subject_digest": cs["subject_digest"],
                "transport": transport,
                "repository": repo["full_name"],
                "clone_url": repo.get("clone_url") or f"https://github.com/{repo['full_name']}.git",
                "target_ref": body.get("target_ref") or target_ref(session["id"], cs["id"]),
                "base_branch": body.get("base_branch")
                or policy.get("base_branch")
                or repo.get("base_ref")
                or "main",
                "draft": bool(body.get("draft", policy.get("draft", True))),
                "title": str(body.get("title") or f"SBX: {session['title'] or cs['id']}")[:200],
                "connection_id": con["id"],
                "authorizing_principal": actor,
                "authorization_kind": authorization,
                "policy": policy,
            },
        )
        uow.append_event(
            session,
            "delivery.requested",
            {
                "delivery_id": d["id"],
                "subject_digest": d["subject_digest"],
                "transport": transport,
                "repository": d["repository"],
                "target_ref": d["target_ref"],
                "authorized_via": authorization,
            },
            actor=actor,
            delivery_id=d["id"],
            changeset_id=cs["id"],
        )
        uow.enqueue_job(
            workspace_id=cs["workspace_id"],
            kind="delivery.perform",
            target_id=d["id"],
            session_id=session["id"],
        )
        return d

    def retry(self, principal: Principal, delivery_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            d = access.owned(uow, principal, "deliveries", delivery_id, lock=True, what="delivery")
            if d["state"] not in ("failed", "blocked"):
                raise DomainError("invalid_transition", f"delivery is {d['state']}")
            d = uow.update(
                "deliveries",
                delivery_id,
                {
                    "state": "pending" if d["state"] == "failed" else d["state"],
                    "state_reason": None,
                    "updated_at": uow.now(),
                },
                bump_version=True,
            )
            uow.enqueue_job(
                workspace_id=d["workspace_id"],
                kind="delivery.perform",
                target_id=delivery_id,
                session_id=d["session_id"],
            )
            return {"delivery": self.view(uow, d)}

        return self.tx.run(fn)

    def refresh(self, principal: Principal, delivery_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            d = access.owned(uow, principal, "deliveries", delivery_id, what="delivery")
            job = uow.enqueue_job(
                workspace_id=d["workspace_id"],
                kind="delivery.reconcile",
                target_id=delivery_id,
                session_id=d["session_id"],
            )
            return {"job_id": job}

        return self.tx.run(fn)

    def request_merge(
        self,
        principal: Principal,
        delivery_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            d = access.owned(uow, principal, "deliveries", delivery_id, lock=True, what="delivery")
            replay = uow.dedupe_begin(
                principal_id=principal.user_id,
                workspace_id=d["workspace_id"],
                command_kind=f"merge_requests.create:{delivery_id}",
                key=idempotency_key,
                request=body,
            )
            if replay is not None:
                return replay
            for field in ("expected_head_sha", "expected_version", "subject_digest"):
                if not body.get(field):
                    raise DomainError(
                        "validation_failed",
                        f"{field} is required (exact-subject merge)",
                        details={"field": field},
                    )
            if uow.count(
                "merge_requests", {"delivery_id": delivery_id, "state": ["pending", "executing"]}
            ):
                raise DomainError("version_conflict", "a merge request is already active")
            mr = uow.insert(
                "merge_requests",
                {
                    "id": new_id("merge_request"),
                    "workspace_id": d["workspace_id"],
                    "delivery_id": delivery_id,
                    "expected_head_sha": body["expected_head_sha"],
                    "expected_delivery_version": int(body["expected_version"]),
                    "subject_digest": body["subject_digest"],
                    "method": body.get("method") or "squash",
                    "mark_ready": bool(body.get("mark_ready")),
                    "authorizing_principal": principal.user_id,
                },
            )
            session = uow.get("sessions", d["session_id"], lock=True)
            uow.append_event(
                session,
                "delivery.merge_requested",
                {
                    "delivery_id": delivery_id,
                    "merge_request_id": mr["id"],
                    "expected_head_sha": mr["expected_head_sha"],
                    "subject_digest": mr["subject_digest"],
                },
                actor=principal.user_id,
                delivery_id=delivery_id,
            )
            uow.enqueue_job(
                workspace_id=d["workspace_id"],
                kind="delivery.merge",
                target_id=mr["id"],
                session_id=d["session_id"],
            )
            response = {
                "merge_request_id": mr["id"],
                "state": mr["state"],
                "event_watermark": uow.watermark(d["session_id"]),
            }
            uow.dedupe_finish(
                principal_id=principal.user_id,
                workspace_id=d["workspace_id"],
                command_kind=f"merge_requests.create:{delivery_id}",
                key=idempotency_key,
                response=response,
            )
            return response

        return self.tx.run(fn)

    # ------------------------------------------------------------------ jobs
    def _step(
        self,
        uow: Any,
        delivery_id: str,
        kind: str,
        outcome: str,
        expected: dict[str, Any],
        evidence: dict[str, Any],
        credential_version_id: str | None,
    ) -> None:
        uow.insert(
            "delivery_steps",
            {
                "id": new_id("delivery_step"),
                "delivery_id": delivery_id,
                "kind": kind,
                "effect_id": f"{delivery_id}:{kind}",
                "outcome": outcome,
                "expected": expected,
                "evidence": evidence,
                "credential_version_id": credential_version_id,
            },
        )

    def _set(
        self,
        uow: Any,
        delivery_id: str,
        state: str,
        *,
        reason: str | None = None,
        event: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        d = uow.get("deliveries", delivery_id, lock=True)
        values = {"state": state, "state_reason": reason, "updated_at": uow.now(), **(extra or {})}
        d = uow.update("deliveries", delivery_id, values, bump_version=True)
        if event:
            session = uow.get("sessions", d["session_id"], lock=True)
            uow.append_event(
                session,
                event,
                {
                    "delivery_id": delivery_id,
                    "state": state,
                    "reason": reason,
                    "commit_sha": d["commit_sha"],
                    "pull_request": d["pr_url"],
                    "subject_digest": d["subject_digest"],
                },
                actor="application",
                delivery_id=delivery_id,
                changeset_id=d["changeset_id"],
            )

    def handle_perform(self, ctx: Any) -> Outcome:
        delivery_id = ctx.claim.job["delivery_id"]
        d = ctx.db.read(lambda uow: uow.get("deliveries", delivery_id))
        if d["state"] in ("succeeded", "cancelled"):
            return Succeeded({"state": d["state"]})
        target = f"{d['repository']}#{d['target_ref']}"

        def begin(uow: Any) -> dict[str, Any]:
            row = uow.get("deliveries", delivery_id, lock=True)
            claim = uow.find_one("delivery_target_claims", {"target": target}, lock=True)
            if claim is None:
                uow.insert(
                    "delivery_target_claims", {"target": target, "holder_delivery_id": delivery_id}
                )
            elif claim["holder_delivery_id"] != delivery_id:
                other = (
                    uow.get("deliveries", claim["holder_delivery_id"])
                    if claim["holder_delivery_id"]
                    else None
                )
                if other is not None and other["state"] == "executing":
                    raise DomainError(
                        "delivery_unresolved",
                        "another Delivery holds this target",
                        retryable=True,
                        retry_after=5,
                    )
                uow.update_where(
                    "delivery_target_claims",
                    {"target": target},
                    {
                        "generation": claim["generation"] + 1,
                        "holder_delivery_id": delivery_id,
                        "updated_at": uow.now(),
                    },
                )
            if row["state"] in ("pending", "blocked", "failed"):
                if row["state"] == "failed":
                    uow.update("deliveries", delivery_id, {"state": "pending"})
                self._set(uow, delivery_id, "executing", event="delivery.progressed")
            cs = uow.get("changesets", row["changeset_id"])
            return {"cs": cs, "patch_key": uow.get("blobs", cs["patch_blob_id"])["storage_key"]}

        try:
            ctx_data = ctx.commit(begin)
        except DomainError as exc:
            return Retry(exc.code, exc.message, retry_after=exc.retry_after)
        cs = ctx_data["cs"]
        try:
            token, meta = self.broker.source_token(d["workspace_id"], d["connection_id"])
            ref = f"refs/heads/{d['target_ref']}"
            remote = self.git.ls_remote(d["clone_url"], token, ref)
            message = (
                f"{d['title']}\n\nSBX-ChangeSet: {cs['id']}\nSBX-Subject: {cs['subject_digest']}"
            )
            timestamp = cs["created_at"].strftime("%Y-%m-%dT%H:%M:%S+0000")
            planned = self.git.materialize_and_push(
                clone_url=d["clone_url"],
                token=token,
                base_sha=cs["base_sha"],
                patch=self.blobs.get(ctx_data["patch_key"]),
                expected_tree=cs["tree_sha"],
                message=message,
                timestamp=timestamp,
                ref=d["target_ref"],
                expected_old=None,
                push=False,
            )
            commit = planned["commit"]
            ctx.commit(
                lambda uow: self._step(
                    uow,
                    delivery_id,
                    "materialize",
                    "verified",
                    {"base_sha": cs["base_sha"], "tree_sha": cs["tree_sha"]},
                    {"commit": commit, "tree": planned["tree"], "deterministic": True},
                    meta["credential_version_id"],
                )
            )
            if remote is None:
                self.git.materialize_and_push(
                    clone_url=d["clone_url"],
                    token=token,
                    base_sha=cs["base_sha"],
                    patch=self.blobs.get(ctx_data["patch_key"]),
                    expected_tree=cs["tree_sha"],
                    message=message,
                    timestamp=timestamp,
                    ref=d["target_ref"],
                    expected_old=None,
                )
            elif remote != commit:
                ctx.commit(
                    lambda uow: (
                        self._step(
                            uow,
                            delivery_id,
                            "push",
                            "blocked",
                            {"expected_old": None, "new": commit},
                            {"remote": remote},
                            meta["credential_version_id"],
                        ),
                        self._set(
                            uow,
                            delivery_id,
                            "blocked",
                            reason="remote_head_changed",
                            event="delivery.blocked",
                        ),
                    )
                )
                return Succeeded({"blocked": "remote_head_changed"})
            verified = self.git.ls_remote(d["clone_url"], token, ref)
            if verified != commit:
                return Retry("delivery_unresolved", "remote ref not yet at intended commit")
            ctx.commit(
                lambda uow: (
                    self._step(
                        uow,
                        delivery_id,
                        "push",
                        "verified",
                        {"ref": d["target_ref"], "new": commit},
                        {"remote": verified, "already_present": remote == commit},
                        meta["credential_version_id"],
                    ),
                    self._set(
                        uow,
                        delivery_id,
                        "executing",
                        extra={
                            "commit_sha": commit,
                            "remote_head_sha": verified,
                            "expected_old_head": remote,
                        },
                    ),
                )
            )
            pr = None
            if d["transport"] == "pull_request":
                pr = self.host.find_pr(d["repository"], token, d["target_ref"])
                if pr is None:
                    body = f"Delivered by SBX from ChangeSet `{cs['id']}`.\n\nSubject: `{cs['subject_digest']}`\n\n<!-- sbx-delivery:{d['id']} -->"
                    pr = self.host.create_pr(
                        d["repository"],
                        token,
                        head_ref=d["target_ref"],
                        base=d["base_branch"],
                        title=d["title"],
                        body=body,
                        draft=d["draft"],
                    )
                if pr["head_sha"] != commit:
                    ctx.commit(
                        lambda uow: self._set(
                            uow,
                            delivery_id,
                            "blocked",
                            reason="remote_head_changed",
                            event="delivery.blocked",
                        )
                    )
                    return Succeeded({"blocked": "pr_head_mismatch"})
                ctx.commit(
                    lambda uow: self._step(
                        uow,
                        delivery_id,
                        "pull_request",
                        "verified",
                        {"head": commit},
                        {
                            "number": pr["number"],
                            "url": pr["url"],
                            "discovered": "sbx-delivery:" + d["id"] in pr.get("body", ""),
                        },
                        meta["credential_version_id"],
                    )
                )
            extra = (
                {
                    "pr_number": pr["number"],
                    "pr_url": pr["url"],
                    "pr_state": pr["state"],
                    "pr_draft": pr["draft"],
                    "observed_at": None,
                }
                if pr
                else {}
            )
            ctx.commit(
                lambda uow: self._set(
                    uow, delivery_id, "succeeded", event="delivery.succeeded", extra=extra
                )
            )
            return Succeeded({"commit": commit})
        except DomainError as exc:
            if exc.retryable and ctx.claim.attempts < 5:
                return Retry(exc.code, exc.message, retry_after=exc.retry_after)
            state = (
                "blocked"
                if exc.code in ("remote_head_changed", "connection_revoked", "credential_invalid")
                else "failed"
            )
            ctx.commit(
                lambda uow, e=exc: self._set(
                    uow,
                    delivery_id,
                    state,
                    reason=e.code,
                    event="delivery.blocked" if state == "blocked" else "delivery.failed",
                )
            )
            return Succeeded({state: exc.code})

    def _observe(self, ctx: Any, d: dict[str, Any]) -> dict[str, Any]:
        token, _ = self.broker.source_token(d["workspace_id"], d["connection_id"])
        values: dict[str, Any] = {}
        if d["pr_number"]:
            pr = self.host.get_pr(d["repository"], token, d["pr_number"])
            checks = self.host.checks(d["repository"], token, pr["head_sha"])
            failing = any(
                c["conclusion"] in ("failure", "error", "cancelled", "timed_out") for c in checks
            )
            pending = any(
                c["conclusion"] in (None, "pending", "queued", "in_progress") for c in checks
            )
            values = {
                "remote_head_sha": pr["head_sha"],
                "pr_state": pr["state"],
                "pr_draft": pr["draft"],
                "checks": checks,
                "checks_state": "failure"
                if failing
                else "pending"
                if pending
                else ("success" if checks else "none"),
            }
        else:
            values = {
                "remote_head_sha": self.git.ls_remote(
                    d["clone_url"], token, f"refs/heads/{d['target_ref']}"
                )
            }

        def commit(uow: Any) -> dict[str, Any]:
            return uow.update("deliveries", d["id"], {**values, "observed_at": uow.now()})

        return ctx.commit(commit)

    def handle_reconcile(self, ctx: Any) -> Outcome:
        d = ctx.db.read(lambda uow: uow.get("deliveries", ctx.claim.job["delivery_id"]))
        if d["state"] != "succeeded":
            return Succeeded({"skipped": d["state"]})
        self._observe(ctx, d)
        return Succeeded()

    def handle_merge(self, ctx: Any) -> Outcome:
        mr_id = ctx.claim.job["merge_request_id"]
        mr = ctx.db.read(lambda uow: uow.get("merge_requests", mr_id))
        if mr["state"] not in ("pending", "executing"):
            return Succeeded({"state": mr["state"]})
        d = ctx.db.read(lambda uow: uow.get("deliveries", mr["delivery_id"]))
        try:
            if d["state"] == "succeeded":
                d = self._observe(ctx, d)
        except DomainError as exc:
            return Retry(exc.code, exc.message)

        def gate(uow: Any) -> dict[str, Any]:
            current = uow.get("deliveries", d["id"], lock=True)
            cs = uow.get("changesets", current["changeset_id"])
            # Effective gate: pinned intent policy AND current mandatory Project policy.
            session = uow.get("sessions", current["session_id"])
            policy = dict(current["policy"])
            if session["project_id"]:
                project = uow.get("projects", session["project_id"])
                latest = (
                    uow.get("project_versions", project["current_version_id"])["spec"].get(
                        "ship_policy"
                    )
                    or {}
                )
                policy["required_checks"] = sorted(
                    set(policy.get("required_checks") or [])
                    | set(latest.get("required_checks") or [])
                )
                policy["required_results"] = _stricter(
                    policy.get("required_results") or [], latest.get("required_results") or []
                )
            result = evaluate_merge_gate(
                changeset=cs,
                delivery=current,
                merge_request=mr,
                results=self._results(uow, current),
                policy=policy,
                now=uow.now(),
            )
            state = "executing" if result["eligible"] else "blocked"
            uow.update(
                "merge_requests", mr_id, {"state": state, "gate": result, "updated_at": uow.now()}
            )
            if not result["eligible"]:
                s = uow.get("sessions", current["session_id"], lock=True)
                uow.append_event(
                    s,
                    "delivery.merge_failed",
                    {"merge_request_id": mr_id, "blocked": True, "reasons": result["reasons"]},
                    actor="application",
                    delivery_id=current["id"],
                )
            return result

        result = ctx.commit(gate)
        if not result["eligible"]:
            return Succeeded({"blocked": result["reasons"]})
        try:
            token, meta = self.broker.source_token(d["workspace_id"], d["connection_id"])
            if d["pr_draft"] and mr["mark_ready"]:
                self.host.mark_ready(
                    d["repository"], token, self.host.get_pr(d["repository"], token, d["pr_number"])
                )
            merged = self.host.merge(
                d["repository"],
                token,
                d["pr_number"],
                sha=mr["expected_head_sha"],
                method=mr["method"],
            )
        except DomainError as exc:
            ctx.commit(lambda uow, e=exc: self._merge_done(uow, mr_id, False, error=e.code))
            return Succeeded({"failed": exc.code})
        ctx.commit(
            lambda uow: self._merge_done(
                uow,
                mr_id,
                merged["merged"],
                merge_sha=merged.get("sha"),
                credential_version_id=meta["credential_version_id"],
            )
        )
        return Succeeded({"merged": merged["merged"]})

    def _merge_done(
        self,
        uow: Any,
        mr_id: str,
        ok: bool,
        *,
        merge_sha: str | None = None,
        error: str | None = None,
        credential_version_id: str | None = None,
    ) -> None:
        mr = uow.update(
            "merge_requests",
            mr_id,
            {
                "state": "succeeded" if ok else "failed",
                "merge_sha": merge_sha,
                "error": error,
                "updated_at": uow.now(),
            },
        )
        d = uow.get("deliveries", mr["delivery_id"], lock=True)
        self._step(
            uow,
            d["id"],
            "merge",
            "verified" if ok else "failed",
            {"expected_head_sha": mr["expected_head_sha"], "method": mr["method"]},
            {"merge_sha": merge_sha, "error": error},
            credential_version_id,
        )
        if ok:
            uow.update("deliveries", d["id"], {"pr_state": "merged", "updated_at": uow.now()})
        session = uow.get("sessions", d["session_id"], lock=True)
        uow.append_event(
            session,
            "delivery.merged" if ok else "delivery.merge_failed",
            {
                "merge_request_id": mr_id,
                "merge_sha": merge_sha,
                "error": error,
                "subject_digest": mr["subject_digest"],
            },
            actor="application",
            delivery_id=d["id"],
        )

    def dependents(self, uow: Any, con: dict[str, Any]) -> dict[str, Any]:
        busy = uow.find("deliveries", {"connection_id": con["id"], "state": "executing"})
        return {"deliveries": [d["id"] for d in busy]} if busy else {}

    def on_changeset_ready(self, uow: Any, session: dict[str, Any], cs: dict[str, Any]) -> None:
        """Automatic delivery only for eligible subjects under an explicit policy grant."""
        policy = (session["effective_spec"] or {}).get("ship_policy") or {}
        if (
            policy.get("automatic_delivery")
            and cs["automatic_eligible"]
            and session["source_connection_id"]
        ):
            self.create_in(uow, "application", cs, {}, authorization="automatic")


def _stricter(pinned: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {r["kind"]: dict(r) for r in pinned}
    for r in current:
        prior = merged.get(r["kind"])
        if prior is None or int(r.get("count") or 1) > int(prior.get("count") or 1):
            merged[r["kind"]] = dict(r)
    return list(merged.values())
