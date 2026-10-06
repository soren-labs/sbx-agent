"""Delegation application: generic child Sessions and validated results (RFC 05).

Review/test/research/integration are ordinary Sessions. The platform validates
the child's completing Turn output against the pinned ResultContract and
publishes one immutable typed result; it never infers approval from text.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from protocol.manifests import content_digest

from control.application import access
from control.application.ports import RuntimeRefused, RuntimeUnavailable
from control.domain.delegation import (
    MAX_CHILDREN,
    MAX_DEPTH,
    ROLE_CONTRACT,
    build_contract,
    extract_result,
    validate_result,
)
from control.domain.digests import digest_of
from control.domain.errors import DomainError
from control.domain.identity import Principal
from control.domain.ids import new_id
from control.jobs.model import Outcome, Retry, Succeeded

INSTRUCTIONS = Path(__file__).resolve().parents[2] / "resources" / "instructions"
DELEGATION_ROLES = ("review", "test", "research", "security", "integration")


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _role_text(role: str) -> str:
    path = INSTRUCTIONS / f"{'review' if role == 'security' else role}.md"
    return path.read_text() if path.exists() else ""


class Delegations:
    def __init__(self, tx: Any, sessions: Any, connector: Any, blobs: Any) -> None:
        self.tx = tx
        self.sessions = sessions
        self.connector = connector
        self.blobs = blobs

    # ------------------------------------------------------------------ views
    def view(self, uow: Any, d: dict[str, Any]) -> dict[str, Any]:
        result = uow.find_one("delegation_results", {"delegation_id": d["id"]})
        child = uow.get("sessions", d["child_session_id"])
        inputs = uow.find("delegation_inputs", {"delegation_id": d["id"]}, order="created_at")
        return {
            "id": d["id"],
            "parent_session_id": d["parent_session_id"],
            "child_session_id": d["child_session_id"],
            "child": {
                "lifecycle": child["lifecycle"],
                "harness": {
                    "provider_id": child["harness_provider"],
                    "model": child["harness_model"],
                },
            },
            "role": d["role"],
            "state": d["state"],
            "state_reason": d["state_reason"],
            "result_contract": {
                k: v for k, v in d["result_contract"].items() if k != "instructions"
            },
            "subject": {
                "changeset_id": d["subject_changeset_id"],
                "subject_digest": d["subject_digest"],
            },
            "inputs": [
                {
                    "kind": i["kind"],
                    "ref": i["ref"],
                    "digest": i["digest"],
                    "applied": i["applied_at"] is not None,
                }
                for i in inputs
            ],
            "result": self._result_view(result) if result else None,
            "depth": d["depth"],
            "created_at": _iso(d["created_at"]),
        }

    @staticmethod
    def _result_view(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": r["id"],
            "kind": r["kind"],
            "verdict": r["verdict"],
            "subject_digest": r["subject_digest"],
            "independent": r["independent"],
            "validation_status": r["validation_status"],
            "completing_turn_id": r["completing_turn_id"],
            "value": r["value"],
            "evidence": r["evidence"],
            "published_at": _iso(r["published_at"]),
        }

    def get(self, principal: Principal, delegation_id: str) -> dict[str, Any]:
        return self.tx.read(
            lambda uow: self.view(
                uow, access.owned(uow, principal, "delegations", delegation_id, what="delegation")
            )
        )

    def list(self, principal: Principal, session_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            access.owned(uow, principal, "sessions", session_id, what="session")
            return {
                "items": [
                    self.view(uow, d)
                    for d in uow.find(
                        "delegations", {"parent_session_id": session_id}, order="created_at"
                    )
                ]
            }

        return self.tx.read(fn)

    def result(self, principal: Principal, delegation_id: str) -> dict[str, Any]:
        view = self.get(principal, delegation_id)
        return {"delegation_id": delegation_id, "state": view["state"], "result": view["result"]}

    # --------------------------------------------------------------- commands
    def spawn(
        self,
        principal: Principal,
        parent_session_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        limits: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            parent = access.owned(
                uow, principal, "sessions", parent_session_id, lock=True, what="session"
            )
            replay = uow.dedupe_begin(
                principal_id=principal.user_id,
                workspace_id=parent["workspace_id"],
                command_kind=f"delegations.spawn:{parent_session_id}",
                key=idempotency_key,
                request=body,
            )
            if replay is not None:
                return replay
            response = self.spawn_in(uow, principal, parent, body, limits=limits or {})
            uow.dedupe_finish(
                principal_id=principal.user_id,
                workspace_id=parent["workspace_id"],
                command_kind=f"delegations.spawn:{parent_session_id}",
                key=idempotency_key,
                response=response,
            )
            return response

        return self.tx.run(fn)

    def spawn_in(
        self,
        uow: Any,
        principal: Principal,
        parent: dict[str, Any],
        body: dict[str, Any],
        *,
        limits: dict[str, int],
    ) -> dict[str, Any]:
        if parent["lifecycle"] != "open":
            raise DomainError("invalid_transition", "parent Session must be open to spawn")
        role = body.get("role")
        if role not in DELEGATION_ROLES:
            raise DomainError("validation_failed", f"role must be one of {DELEGATION_ROLES}")
        parent_link = uow.find_one("delegations", {"child_session_id": parent["id"]})
        depth = (parent_link["depth"] if parent_link else 0) + 1
        if depth > min(MAX_DEPTH, limits.get("max_depth", MAX_DEPTH)):
            raise DomainError("quota_exhausted", "delegation depth budget exceeded")
        children = uow.count("delegations", {"parent_session_id": parent["id"]})
        if children >= min(MAX_CHILDREN, limits.get("max_children", MAX_CHILDREN)):
            raise DomainError("quota_exhausted", "delegation children budget exceeded")
        subject = None
        if body.get("changeset_id"):
            subject = uow.get(
                "changesets", body["changeset_id"], workspace_ids=[parent["workspace_id"]]
            )
            if subject is None:
                raise DomainError("not_found", "changeset not found")
            if subject["state"] != "ready":
                raise DomainError("stale_subject", "Delegation inputs must be sealed ChangeSets")
        spec = parent["effective_spec"]
        checks = spec.get("checks") or []
        kind = body.get("result_kind") or ROLE_CONTRACT.get(role, "GenericResult")
        contract = build_contract(
            kind,
            subject_digest=subject["subject_digest"] if subject else None,
            platform_checks=checks if kind == "TestResult" else None,
        )
        repository = spec.get("repository")
        child_body: dict[str, Any] = {
            "role": role,
            "title": str(
                body.get("title") or f"{role.title()} of {parent['title'] or parent['id']}"
            )[:200],
            "harness": body.get("harness")
            or {"provider_id": parent["harness_provider"], "model": parent["harness_model"]},
            "executor": {
                "backend": parent["executor_backend"],
                "resource_class": parent["resource_class"],
            },
            "connections": {
                "compute": parent["compute_connection_id"],
                "source": parent["source_connection_id"],
                **({} if body.get("harness") else {"inference": parent["inference_connection_id"]}),
            },
            "repository": repository,
        }
        if parent["project_version_id"] and not body.get("harness"):
            child_body["project_version_id"] = parent["project_version_id"]
        created = self.sessions.create_in(
            uow,
            principal,
            parent["workspace_id"],
            child_body,
            parent_session_id=parent["id"],
            actor=principal.user_id,
        )
        child = uow.get("sessions", created["session_id"], lock=True)
        if subject is not None:
            uow.update_where(
                "worktrees",
                {"session_id": child["id"]},
                {"base_sha": subject["base_sha"], "repository": subject["repository"]},
            )
        delegation = uow.insert(
            "delegations",
            {
                "id": new_id("delegation"),
                "workspace_id": parent["workspace_id"],
                "parent_session_id": parent["id"],
                "child_session_id": child["id"],
                "role": role,
                "state": "active",
                "result_contract": contract,
                "contract_digest": digest_of(contract),
                "subject_changeset_id": subject["id"] if subject else None,
                "subject_digest": subject["subject_digest"] if subject else None,
                "depth": depth,
                "budget": {"max_depth": MAX_DEPTH, "max_children": MAX_CHILDREN, **limits},
                "created_by": principal.user_id,
            },
        )
        summary = str(body.get("context") or "").strip()
        if subject is not None:
            uow.insert(
                "delegation_inputs",
                {
                    "id": new_id("delegation"),
                    "delegation_id": delegation["id"],
                    "kind": "changeset",
                    "ref": subject["id"],
                    "digest": subject["subject_digest"],
                    "apply_to_worktree": True,
                },
            )
        if summary:
            uow.insert(
                "delegation_inputs",
                {
                    "id": new_id("delegation"),
                    "delegation_id": delegation["id"],
                    "kind": "summary",
                    "ref": "context",
                    "digest": digest_of(summary),
                },
            )
        prompt = _role_text(role)
        if summary:
            prompt += f"\n\nContext from the parent Session:\n{summary}"
        if subject is not None:
            prompt += f"\n\nSubject ChangeSet {subject['id']} ({subject['file_count']} files) is applied as HEAD in your copy."
        if body.get("instructions"):
            prompt += "\n\n" + str(body["instructions"])
        accepted = self.sessions._accept(
            uow,
            None,
            child,
            {"content": prompt.strip()},
            actor=principal.user_id,
            author_kind="session",
            source_session_id=parent["id"],
            result_contract=contract,
        )
        for target in (parent, child):
            uow.append_event(
                target,
                "delegation.created",
                {
                    "delegation_id": delegation["id"],
                    "role": role,
                    "child_session_id": child["id"],
                    "parent_session_id": parent["id"],
                    "subject_digest": delegation["subject_digest"],
                    "contract": contract["kind"],
                },
                actor=principal.user_id,
                delegation_id=delegation["id"],
            )
        return {
            "delegation_id": delegation["id"],
            "child_session_id": child["id"],
            "turn_id": accepted["turn_id"],
            "event_watermark": uow.watermark(parent["id"]),
        }

    def send(
        self,
        principal: Principal,
        delegation_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        d = self.get(principal, delegation_id)
        return self.sessions.send(
            principal,
            d["child_session_id"],
            {**body, "reply_to_message_id": body.get("reply_to_message_id")},
            idempotency_key=idempotency_key,
        )

    def wait(
        self,
        principal: Principal,
        delegation_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        wake = body.get("wake") or "none"
        if wake not in ("message", "none"):
            raise DomainError("validation_failed", "wake must be message or none")

        def fn(uow: Any) -> dict[str, Any]:
            d = access.owned(
                uow, principal, "delegations", delegation_id, lock=True, what="delegation"
            )
            replay = uow.dedupe_begin(
                principal_id=principal.user_id,
                workspace_id=d["workspace_id"],
                command_kind=f"delegations.wait:{delegation_id}",
                key=idempotency_key,
                request=body,
            )
            if replay is not None:
                return replay
            deadline = uow.now() + __import__("datetime").timedelta(
                seconds=float(body.get("deadline_seconds") or 86400)
            )
            sub = uow.insert(
                "wait_subscriptions",
                {
                    "id": new_id("wait"),
                    "workspace_id": d["workspace_id"],
                    "delegation_id": delegation_id,
                    "waiter_session_id": d["parent_session_id"],
                    "wake": wake,
                    "deadline_at": deadline,
                },
            )
            parent = uow.get("sessions", d["parent_session_id"], lock=True)
            uow.append_event(
                parent,
                "delegation.waiting",
                {"delegation_id": delegation_id, "subscription_id": sub["id"], "wake": wake},
                actor=principal.user_id,
                delegation_id=delegation_id,
            )
            if d["state"] in ("succeeded", "failed", "cancelled"):
                # Registration checks availability in the same transaction: no missed wakeup.
                self._satisfy(
                    uow, d, uow.find_one("delegation_results", {"delegation_id": delegation_id})
                )
            sub = uow.get("wait_subscriptions", sub["id"])
            response = {
                "subscription_id": sub["id"],
                "state": sub["state"],
                "delegation_state": d["state"],
            }
            uow.dedupe_finish(
                principal_id=principal.user_id,
                workspace_id=d["workspace_id"],
                command_kind=f"delegations.wait:{delegation_id}",
                key=idempotency_key,
                response=response,
            )
            return response

        return self.tx.run(fn)

    def cancel(self, principal: Principal, delegation_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            d = access.owned(
                uow, principal, "delegations", delegation_id, lock=True, what="delegation"
            )
            self.cancel_in(uow, d, actor=principal.user_id, reason="cancelled_by_parent")
            return self.view(uow, uow.get("delegations", delegation_id))

        return self.tx.run(fn)

    def cancel_in(self, uow: Any, d: dict[str, Any], *, actor: str, reason: str) -> None:
        if d["state"] in ("succeeded", "failed", "cancelled"):
            return
        parent = uow.get("sessions", d["parent_session_id"], lock=True)
        child = uow.get("sessions", d["child_session_id"], lock=True)
        uow.append_event(
            parent,
            "delegation.cancel_requested",
            {"delegation_id": d["id"], "reason": reason},
            actor=actor,
            delegation_id=d["id"],
        )
        for grandchild in uow.find(
            "delegations",
            {"parent_session_id": child["id"], "state": ["pending", "active", "waiting_result"]},
            lock=True,
        ):
            self.cancel_in(uow, grandchild, actor=actor, reason=reason)
        if child["lifecycle"] != "closed":
            self.sessions.lifecycle_in(uow, child, "closed", actor=actor)
        uow.update(
            "delegations",
            d["id"],
            {
                "state": "cancelled",
                "state_reason": reason,
                "cancel_requested_at": uow.now(),
                "updated_at": uow.now(),
            },
        )
        for target in (parent, uow.get("sessions", child["id"])):
            uow.append_event(
                target,
                "delegation.cancelled",
                {"delegation_id": d["id"], "reason": reason},
                actor=actor,
                delegation_id=d["id"],
            )
        self._satisfy(uow, uow.get("delegations", d["id"]), None)

    # ---------------------------------------------------------------- hooks
    def on_turn_terminal(
        self, uow: Any, session: dict[str, Any], turn: dict[str, Any], state: str
    ) -> None:
        if not turn["result_contract"]:
            return
        d = uow.find_one(
            "delegations",
            {"child_session_id": session["id"], "state": ["active", "waiting_result"]},
        )
        if d is None:
            return
        uow.update(
            "delegations", d["id"], {"state": "waiting_result", "updated_at": uow.now()}
        ) if d["state"] == "active" else None
        uow.enqueue_job(
            workspace_id=d["workspace_id"],
            kind="delegation.publish_result",
            target_id=d["id"],
            dedupe_key=f"{d['id']}:{turn['id']}",
            input={"turn_id": turn["id"]},
            session_id=d["parent_session_id"],
        )

    def on_parent_close(self, uow: Any, session: dict[str, Any], actor: str) -> None:
        for d in uow.find(
            "delegations",
            {"parent_session_id": session["id"], "state": ["pending", "active", "waiting_result"]},
            lock=True,
        ):
            self.cancel_in(uow, d, actor=actor, reason="parent_closed")

    def apply_inputs(self, ctx: Any, lease: dict[str, Any], session: dict[str, Any]) -> None:
        """After Worktree realization: apply pinned input ChangeSets as the child's baseline."""

        def read(uow: Any) -> list[tuple[Any, Any, Any]]:
            d = uow.find_one("delegations", {"child_session_id": session["id"]})
            if d is None:
                return []
            out = []
            for item in uow.find(
                "delegation_inputs",
                {
                    "delegation_id": d["id"],
                    "kind": "changeset",
                    "apply_to_worktree": True,
                    "applied_at": None,
                },
            ):
                cs = uow.get("changesets", item["ref"])
                out.append((item, cs, uow.get("blobs", cs["patch_blob_id"])))
            return out

        for item, cs, blob in ctx.db.read(read):
            patch = self.blobs.get(blob["storage_key"])
            payload = {
                "changeset_id": cs["id"],
                "patch_b64": base64.b64encode(patch).decode(),
                "patch_digest": content_digest(patch),
                "baseline_tree": cs["baseline_tree"],
                "commit_as_baseline": True,
            }
            response = self.connector.channel(lease).op(
                "changes.apply", f"{lease['id']}:input:{cs['id']}", session["id"], payload
            )
            if response["status"] != "succeeded":
                raise DomainError(
                    "stale_subject",
                    "pinned input ChangeSet could not be applied to the child copy",
                    retryable=False,
                )

            def mark(uow: Any, item: dict[str, Any] = item, cs: dict[str, Any] = cs) -> None:
                uow.update("delegation_inputs", item["id"], {"applied_at": uow.now()})
                child = uow.get("sessions", session["id"], lock=True)
                uow.append_event(
                    child,
                    "worktree.applied",
                    {
                        "changeset_id": cs["id"],
                        "subject_digest": cs["subject_digest"],
                        "as_baseline": True,
                    },
                    actor="application",
                    changeset_id=cs["id"],
                )

            ctx.commit(mark)

    # ------------------------------------------------------------------ jobs
    def handle_publish(self, ctx: Any) -> Outcome:
        delegation_id = ctx.claim.job["delegation_id"]
        turn_id = ctx.input["turn_id"]

        def read(uow: Any) -> tuple[Any, Any, str, Any]:
            d = uow.get("delegations", delegation_id)
            turn = uow.get("turns", turn_id)
            text = ""
            if turn["output_message_id"]:
                parts = uow.find(
                    "message_parts",
                    {"message_id": turn["output_message_id"], "kind": "text"},
                    order="ordinal",
                )
                text = "\n".join(p["content"] for p in parts)
            lease = uow.find_one(
                "executor_leases", {"session_id": d["child_session_id"], "state": "ready"}
            )
            return d, turn, text, lease

        d, turn, text, lease = ctx.db.read(read)
        if d["state"] in ("succeeded", "failed", "cancelled"):
            return Succeeded({"state": d["state"]})
        if turn["state"] != "succeeded":
            ctx.commit(lambda uow: self._fail(uow, delegation_id, f"child_turn_{turn['state']}"))
            return Succeeded({"failed": turn["state"]})
        try:
            typed = validate_result(d["result_contract"], extract_result(text))
        except DomainError as exc:
            ctx.commit(lambda uow, e=exc: self._fail(uow, delegation_id, e.code, e.message))
            return Succeeded({"failed": exc.code})
        evidence: dict[str, Any] = {}
        checks = d["result_contract"].get("evidence_requirements", {}).get("platform_checks") or []
        if checks:
            if lease is None:
                ctx.commit(
                    lambda uow: self._fail(
                        uow,
                        delegation_id,
                        "output_contract_invalid",
                        "platform checks require the child executor",
                    )
                )
                return Succeeded({"failed": "no_executor"})
            runs = []
            try:
                for check in checks:
                    response = self.connector.channel(lease).op(
                        "check.run",
                        f"{delegation_id}:check:{check['name']}",
                        d["child_session_id"],
                        check,
                    )
                    runs.append(response.get("result") or {})
            except (RuntimeUnavailable, RuntimeRefused) as exc:
                return Retry("executor_unavailable", str(exc)[:200])
            evidence["platform_checks"] = runs
            if any(r.get("status") != "passed" for r in runs):
                typed["verdict"] = "fail"
                typed["value"] = {
                    **typed["value"],
                    "status": "fail",
                    "platform_override": "declared check failed",
                }
        ctx.commit(lambda uow: self._publish(uow, delegation_id, turn_id, typed, evidence))
        return Succeeded({"verdict": typed["verdict"]})

    def _independent(self, uow: Any, d: dict[str, Any], turn_id: str) -> bool:
        child_exec = uow.find("executions", {"turn_id": turn_id})
        parent_bindings = {
            b["native_id"]
            for b in uow.find("native_context_bindings", {"session_id": d["parent_session_id"]})
        }
        child_bindings = {
            b["native_id"]
            for b in uow.find("native_context_bindings", {"session_id": d["child_session_id"]})
        }
        parent_wt = uow.find_one("worktrees", {"session_id": d["parent_session_id"]})
        child_wt = uow.find_one("worktrees", {"session_id": d["child_session_id"]})
        return (
            bool(child_exec)
            and not (parent_bindings & child_bindings)
            and parent_wt["id"] != child_wt["id"]
            and d["child_session_id"] != d["parent_session_id"]
        )

    def _publish(
        self,
        uow: Any,
        delegation_id: str,
        turn_id: str,
        typed: dict[str, Any],
        evidence: dict[str, Any],
    ) -> None:
        d = uow.get("delegations", delegation_id, lock=True)
        if d["state"] in ("succeeded", "failed", "cancelled"):
            return
        cs = uow.get("changesets", d["subject_changeset_id"]) if d["subject_changeset_id"] else None
        result = uow.insert(
            "delegation_results",
            {
                "id": new_id("result"),
                "workspace_id": d["workspace_id"],
                "delegation_id": delegation_id,
                "child_session_id": d["child_session_id"],
                "completing_turn_id": turn_id,
                "kind": typed["kind"],
                "contract_version": d["result_contract"]["schema_version"],
                "subject_digest": typed["subject_digest"],
                "head_sha": cs["head_sha"] if cs else None,
                "verdict": typed["verdict"],
                "validation_status": "valid",
                "independent": self._independent(uow, d, turn_id),
                "value": typed["value"],
                "evidence": evidence,
            },
        )
        uow.update("delegations", delegation_id, {"state": "succeeded", "updated_at": uow.now()})
        payload = {
            "delegation_id": delegation_id,
            "result_id": result["id"],
            "kind": result["kind"],
            "verdict": result["verdict"],
            "subject_digest": result["subject_digest"],
            "independent": result["independent"],
        }
        for sid in (d["parent_session_id"], d["child_session_id"]):
            uow.append_event(
                uow.get("sessions", sid, lock=True),
                "delegation.result_published",
                payload,
                actor="application",
                delegation_id=delegation_id,
            )
        self._satisfy(uow, uow.get("delegations", delegation_id), result)

    def _fail(self, uow: Any, delegation_id: str, reason: str, message: str = "") -> None:
        d = uow.get("delegations", delegation_id, lock=True)
        if d["state"] in ("succeeded", "failed", "cancelled"):
            return
        uow.update(
            "delegations",
            delegation_id,
            {"state": "failed", "state_reason": reason, "updated_at": uow.now()},
        )
        for sid in (d["parent_session_id"], d["child_session_id"]):
            uow.append_event(
                uow.get("sessions", sid, lock=True),
                "delegation.failed",
                {"delegation_id": delegation_id, "reason": reason, "message": message[:300]},
                actor="application",
                delegation_id=delegation_id,
            )
        self._satisfy(uow, uow.get("delegations", delegation_id), None)

    def _satisfy(self, uow: Any, d: dict[str, Any], result: dict[str, Any] | None) -> None:
        for sub in uow.find(
            "wait_subscriptions", {"delegation_id": d["id"], "state": "pending"}, lock=True
        ):
            evidence = {
                "delegation_state": d["state"],
                "result_id": result["id"] if result else None,
                "verdict": result["verdict"] if result else None,
            }
            uow.update(
                "wait_subscriptions",
                sub["id"],
                {"state": "satisfied", "satisfied_at": uow.now(), "evidence": evidence},
            )
            fresh = uow.outbox(
                workspace_id=d["workspace_id"],
                destination="wait_wake",
                dedupe_key=f"{sub['id']}:{evidence['result_id'] or d['state']}",
                payload=evidence,
                session_id=sub["waiter_session_id"],
            )
            if fresh and sub["wake"] == "message":
                waiter = uow.get("sessions", sub["waiter_session_id"], lock=True)
                text = f"Delegation {d['id']} ({d['role']}) finished: state={d['state']}, verdict={evidence['verdict']}."
                routing = "queue" if waiter["lifecycle"] == "open" else "note"
                self.sessions._accept(
                    uow,
                    None,
                    waiter,
                    {"content": text, "routing": routing},
                    actor="application",
                    author_kind="session",
                    source_session_id=d["child_session_id"],
                )
