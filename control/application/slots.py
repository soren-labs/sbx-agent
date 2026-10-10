"""Subscription Machine Slots: one independent official login per Slot.

A Slot is a durable row plus one private Volume in the owner's Modal workspace. Logging
in runs the provider's official CLI in a temporary Setup VM; the control plane only
relays the verification URL and the one-time user code, then observes the outcome. The
login itself never leaves the Volume: nothing here reads, stores or returns credentials.

The Slot's ``holder`` is the single VM allowed to mount its Volume (a Setup VM now, a
Session's Worker later), so a login and an execution can never share a writable profile.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from control.application import access
from control.domain.errors import DomainError
from control.domain.identity import Principal
from control.domain.ids import new_id
from control.jobs.model import Continue, Outcome, Retry, Succeeded

LIVE_ATTEMPT_STATES = ("starting", "awaiting_user", "verifying")
SETUP_STATE_PATH = "/tmp/sbx-setup/state.json"
# The Setup VM outlives the login window only long enough to be observed and stopped.
SETUP_VM_MARGIN_SECONDS = 300
_VOLUME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_NEVER_STARTED = "setup_vm_lost"


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _timestamp(value: Any) -> datetime | None:
    """A timestamp reported from inside the VM; anything malformed is dropped."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _text(body: dict[str, Any], field: str, *, limit: int = 80) -> str | None:
    value = body.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or len(value.strip()) > limit:
        raise DomainError(
            "validation_failed",
            f"{field} must be at most {limit} characters",
            details={"field": field},
        )
    return value.strip() or None


class MachineSlots:
    def __init__(
        self,
        tx: Any,
        adapters: dict[str, Any],
        *,
        backend: Any = None,
        compute: Callable[[str], dict[str, Any]] | None = None,
        profile_mount: str = "/profile",
    ) -> None:
        self.tx = tx
        self.adapters = adapters
        # Executor able to run Setup VMs on private Volumes (Modal); ``None`` disables Slots.
        self.backend = backend if hasattr(backend, "setup_start") else None
        self.compute = compute
        self.profile_mount = profile_mount
        # Cadence of the login Job while it waits on the Setup VM.
        self.poll_seconds = 2.0

    # ------------------------------------------------------------------------ views
    def providers(self) -> list[dict[str, Any]]:
        return [
            {**adapter.describe(), "available": self.backend is not None}
            for adapter in self.adapters.values()
        ]

    def view(self, uow: Any, slot: dict[str, Any]) -> dict[str, Any]:
        attempt = (
            uow.get("slot_login_attempts", slot["current_login_attempt_id"])
            if slot["current_login_attempt_id"]
            else None
        )
        live = attempt is not None and attempt["state"] in LIVE_ATTEMPT_STATES
        if slot["state"] in ("deleting", "deleted"):
            status = slot["state"]
        elif slot["holder_kind"] == "worker":
            status = "running"
        elif live:
            status = "login_pending"
        else:
            status = slot["state"]
        adapter = self.adapters.get(slot["provider"])
        return {
            "id": slot["id"],
            "workspace_id": slot["workspace_id"],
            "provider": slot["provider"],
            "provider_name": adapter.display_name if adapter else slot["provider"],
            "label": slot["label"],
            "account_alias": slot["account_alias"],
            "state": slot["state"],
            # What the user sees: running | ready | login_pending | needs_login | error | deleting.
            "status": status,
            "state_reason": slot["state_reason"],
            "busy": slot["holder_kind"] == "worker",
            "worker": {"lease_id": slot["holder_id"]} if slot["holder_kind"] == "worker" else None,
            "compute_connection_id": slot["compute_connection_id"],
            "volume": {
                "name": slot["volume_name"],
                "filesystem": "modal_volume_v2",
                "managed": slot["volume_managed"],
            },
            "login": {
                "attempt_id": attempt["id"],
                "mode": attempt["mode"],
                "state": attempt["state"],
                "verification_url": attempt["verification_url"] if live else None,
                # The one-time code is shown only while it still has to be entered.
                "user_code": attempt["user_code"] if attempt["state"] == "awaiting_user" else None,
                "code_expires_at": _iso(attempt["code_expires_at"]) if live else None,
                "error_code": attempt["error_code"],
                "started_at": _iso(attempt["created_at"]),
                "finished_at": _iso(attempt["finished_at"]),
            }
            if attempt
            else None,
            "capabilities": slot["capabilities"] or {},
            "verified_at": _iso(slot["verified_at"]),
            "last_used_at": _iso(slot["last_used_at"]),
            "version": slot["version"],
            "created_at": _iso(slot["created_at"]),
            "updated_at": _iso(slot["updated_at"]),
        }

    def list(self, principal: Principal, workspace_id: str) -> dict[str, Any]:
        access.require_workspace(principal, workspace_id)

        def fn(uow: Any) -> dict[str, Any]:
            rows = uow.find("machine_slots", {"workspace_id": workspace_id}, order="created_at")
            items = [self.view(uow, row) for row in rows if row["state"] != "deleted"]
            return {
                "items": items,
                "summary": {
                    "total": len(items),
                    "running": sum(1 for i in items if i["status"] == "running"),
                    "ready": sum(1 for i in items if i["status"] == "ready"),
                    "login_pending": sum(1 for i in items if i["status"] == "login_pending"),
                    "needs_attention": sum(
                        1 for i in items if i["status"] in ("needs_login", "error")
                    ),
                },
                "providers": self.providers(),
            }

        return self.tx.read(fn)

    def get(self, principal: Principal, slot_id: str) -> dict[str, Any]:
        return self.tx.read(lambda uow: self.view(uow, self._owned(uow, principal, slot_id)))

    def _owned(
        self, uow: Any, principal: Principal, slot_id: str, *, lock: bool = False
    ) -> dict[str, Any]:
        slot = access.owned(
            uow, principal, "machine_slots", slot_id, lock=lock, what="machine slot"
        )
        if slot["state"] == "deleted":
            raise DomainError("not_found", "machine slot not found")
        return slot

    # --------------------------------------------------------------------- commands
    def create(
        self,
        principal: Principal,
        workspace_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        access.require_workspace(principal, workspace_id)
        provider = body.get("provider")
        adapter = self.adapters.get(provider)
        if adapter is None:
            raise DomainError(
                "validation_failed",
                "unknown subscription provider",
                details={"field": "provider", "supported": sorted(self.adapters)},
            )
        self._require_backend()
        label = _text(body, "label")
        alias = _text(body, "account_alias")
        adopted = body.get("volume_name")
        if adopted is not None and (
            not isinstance(adopted, str) or not _VOLUME_NAME.match(adopted)
        ):
            raise DomainError(
                "validation_failed", "invalid Modal Volume name", details={"field": "volume_name"}
            )
        request = {
            "provider": provider,
            "label": label,
            "account_alias": alias,
            "compute_connection_id": body.get("compute_connection_id"),
            "volume_name": adopted,
        }

        def fn(uow: Any) -> dict[str, Any]:
            replay = uow.dedupe_begin(
                principal_id=principal.user_id,
                workspace_id=workspace_id,
                command_kind="machine_slots.create",
                key=idempotency_key,
                request=request,
            )
            if replay is not None:
                return replay
            connection = self._modal_connection(
                uow, principal, workspace_id, body.get("compute_connection_id")
            )
            slot_id = new_id("machine_slot")
            volume_name = adopted or f"sbx-slot-{slot_id.split('_', 1)[1]}"
            taken = [
                row
                for row in uow.find(
                    "machine_slots",
                    {"compute_connection_id": connection["id"], "volume_name": volume_name},
                )
                if row["state"] != "deleted"
            ]
            if taken:
                raise DomainError(
                    "validation_failed",
                    "this Volume already belongs to a Machine Slot",
                    details={"field": "volume_name"},
                )
            existing = uow.count(
                "machine_slots", {"workspace_id": workspace_id, "provider": provider}
            )
            slot = uow.insert(
                "machine_slots",
                {
                    "id": slot_id,
                    "workspace_id": workspace_id,
                    "created_by": principal.user_id,
                    "provider": provider,
                    "label": label or f"{adapter.display_name} {existing + 1}",
                    "account_alias": alias,
                    "compute_connection_id": connection["id"],
                    "volume_name": volume_name,
                    "volume_managed": adopted is None,
                },
            )
            # An existing Volume is only checked; a new one needs the official login.
            slot = self._begin_attempt(uow, slot, "verify" if adopted else "login")
            self._audit(uow, principal.user_id, "machine_slot.create", slot)
            response = self.view(uow, slot)
            uow.dedupe_finish(
                principal_id=principal.user_id,
                workspace_id=workspace_id,
                command_kind="machine_slots.create",
                key=idempotency_key,
                response=response,
            )
            return response

        return self.tx.run(fn)

    def update(self, principal: Principal, slot_id: str, body: dict[str, Any]) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            slot = self._owned(uow, principal, slot_id, lock=True)
            values: dict[str, Any] = {}
            if "label" in body:
                label = _text(body, "label")
                if not label:
                    raise DomainError(
                        "validation_failed", "label is required", details={"field": "label"}
                    )
                values["label"] = label
            if "account_alias" in body:
                values["account_alias"] = _text(body, "account_alias")
            if not values:
                return self.view(uow, slot)
            slot = uow.update(
                "machine_slots", slot_id, {**values, "updated_at": uow.now()}, bump_version=True
            )
            self._audit(uow, principal.user_id, "machine_slot.update", slot)
            return self.view(uow, slot)

        return self.tx.run(fn)

    def start_login(
        self, principal: Principal, slot_id: str, *, mode: str = "login"
    ) -> dict[str, Any]:
        """Re-login (official device login again) or re-verify the stored login."""
        self._require_backend()

        def fn(uow: Any) -> dict[str, Any]:
            slot = self._owned(uow, principal, slot_id, lock=True)
            slot = self._begin_attempt(uow, slot, mode)
            self._audit(uow, principal.user_id, f"machine_slot.{mode}", slot)
            return self.view(uow, slot)

        return self.tx.run(fn)

    def cancel_login(self, principal: Principal, slot_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            slot = self._owned(uow, principal, slot_id, lock=True)
            attempt = self._live_attempt(uow, slot)
            if attempt is None:
                raise DomainError("invalid_transition", "no login is in progress for this slot")
            uow.update(
                "slot_login_attempts",
                attempt["id"],
                {"cancel_requested": True, "updated_at": uow.now()},
            )
            self._wake(uow, slot, attempt)
            self._audit(uow, principal.user_id, "machine_slot.login_cancel", slot)
            return self.view(uow, slot)

        return self.tx.run(fn)

    def logout(self, principal: Principal, slot_id: str) -> dict[str, Any]:
        """Revoke the stored login by destroying the Slot's Volume; the Slot stays."""
        return self._retire(principal, slot_id, "logout", None)

    def delete(self, principal: Principal, slot_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._retire(principal, slot_id, "delete", body.get("confirm"))

    def _retire(
        self, principal: Principal, slot_id: str, mode: str, confirm: Any
    ) -> dict[str, Any]:
        self._require_backend()

        def fn(uow: Any) -> dict[str, Any]:
            slot = self._owned(uow, principal, slot_id, lock=True)
            if mode == "delete" and confirm != slot["label"]:
                raise DomainError(
                    "validation_failed",
                    "confirm must equal the slot label",
                    details={"field": "confirm"},
                )
            if mode == "logout" and not slot["volume_managed"]:
                raise DomainError(
                    "unsupported_capability",
                    "SBX did not create this Volume; delete the slot or the Volume in Modal instead",
                )
            if slot["state"] == "deleting":
                return self.view(uow, slot)
            if slot["holder_kind"] == "worker":
                raise DomainError(
                    "invalid_transition",
                    "the slot is running a Session; release its executor first",
                    details={"lease_id": slot["holder_id"]},
                )
            attempt = self._live_attempt(uow, slot)
            if attempt is not None:
                uow.update(
                    "slot_login_attempts",
                    attempt["id"],
                    {"cancel_requested": True, "updated_at": uow.now()},
                )
            slot = uow.update(
                "machine_slots",
                slot_id,
                {"state": "deleting", "state_reason": mode, "updated_at": uow.now()},
                bump_version=True,
            )
            uow.enqueue_job(
                workspace_id=slot["workspace_id"],
                kind="slot.delete",
                target_id=slot_id,
                input={"mode": mode},
                max_attempts=50,
            )
            self._audit(uow, principal.user_id, f"machine_slot.{mode}", slot)
            return self.view(uow, slot)

        return self.tx.run(fn)

    def dependents(self, uow: Any, con: dict[str, Any]) -> dict[str, Any]:
        """A Modal Connection stays while Slots keep their Volumes in its workspace."""
        if con["kind"] != "modal":
            return {}
        slots = [
            row["id"]
            for row in uow.find("machine_slots", {"compute_connection_id": con["id"]})
            if row["state"] != "deleted"
        ]
        return {"machine_slots": slots} if slots else {}

    # ---------------------------------------------------------------------- helpers
    def _require_backend(self) -> None:
        if self.backend is None or self.compute is None:
            raise DomainError(
                "unsupported_capability",
                "Machine Slots need the Modal executor; it is not enabled on this deployment",
            )

    def _modal_connection(
        self, uow: Any, principal: Principal, workspace_id: str, connection_id: Any
    ) -> dict[str, Any]:
        if connection_id:
            con = access.owned(uow, principal, "connections", str(connection_id), what="connection")
            candidates = [con] if con["workspace_id"] == workspace_id else []
        else:
            candidates = uow.find(
                "connections", {"workspace_id": workspace_id, "kind": "modal"}, order="created_at"
            )
        usable = [
            c
            for c in candidates
            if c["kind"] == "modal" and c["config_state"] == "configured" and c["health"] == "ready"
        ]
        if not usable:
            raise DomainError(
                "connection_required",
                "a verified Modal connection is required to host the slot",
                details={"kind": "modal"},
            )
        return usable[0]

    def _live_attempt(self, uow: Any, slot: dict[str, Any]) -> dict[str, Any] | None:
        if not slot["current_login_attempt_id"]:
            return None
        attempt = uow.get("slot_login_attempts", slot["current_login_attempt_id"], lock=True)
        return attempt if attempt["state"] in LIVE_ATTEMPT_STATES else None

    def _begin_attempt(self, uow: Any, slot: dict[str, Any], mode: str) -> dict[str, Any]:
        if slot["state"] in ("deleting", "deleted"):
            raise DomainError("invalid_transition", "the slot is being deleted")
        if slot["holder_kind"] is not None:
            raise DomainError(
                "invalid_transition",
                "the slot is busy"
                + (" running a Session" if slot["holder_kind"] == "worker" else " with a login"),
                details={"holder": slot["holder_kind"]},
            )
        adapter = self.adapters[slot["provider"]]
        now = uow.now()
        attempt = uow.insert(
            "slot_login_attempts",
            {
                "id": new_id("slot_login"),
                "workspace_id": slot["workspace_id"],
                "slot_id": slot["id"],
                "mode": mode,
                "operation_id": new_id("operation"),
                "deadline_at": now + timedelta(seconds=adapter.login_window_seconds + 120),
            },
        )
        slot = uow.update(
            "machine_slots",
            slot["id"],
            {
                "current_login_attempt_id": attempt["id"],
                "holder_kind": "setup",
                "holder_id": attempt["id"],
                "holder_generation": slot["holder_generation"] + 1,
                "updated_at": now,
            },
            bump_version=True,
        )
        self._wake(uow, slot, attempt)
        return slot

    def _wake(self, uow: Any, slot: dict[str, Any], attempt: dict[str, Any]) -> None:
        uow.enqueue_job(
            workspace_id=slot["workspace_id"],
            kind="slot.login",
            target_id=slot["id"],
            dedupe_key=attempt["id"],
            input={"attempt_id": attempt["id"]},
            # Polling Continues count as attempts; the attempt's own deadline bounds the Job.
            max_attempts=100000,
        )

    def _audit(
        self, uow: Any, actor: str, action: str, slot: dict[str, Any], result: str = "ok"
    ) -> None:
        uow.audit(
            actor=actor,
            action=action,
            purpose=slot["provider"],
            target_kind="machine_slot",
            target_id=slot["id"],
            target_version=slot["version"],
            result=result,
            workspace_id=slot["workspace_id"],
        )

    # ------------------------------------------------------------------------- jobs
    def handle_login(self, ctx: Any) -> Outcome:
        """Drive one login attempt: start the Setup VM, relay the code, observe, clean up."""
        attempt_id = ctx.input["attempt_id"]
        attempt, slot = ctx.db.read(
            lambda uow: (
                (a := uow.get("slot_login_attempts", attempt_id)),
                uow.get("machine_slots", a["slot_id"]),
            )
        )
        if attempt["state"] not in LIVE_ATTEMPT_STATES:
            return Succeeded({"state": attempt["state"]})
        try:
            compute = self.compute(slot["compute_connection_id"])
        except DomainError as exc:
            # Without the Modal credential nothing can be observed or stopped any more; the
            # Setup VM ends by its own hard timeout.
            return self._conclude(ctx, attempt, None, "failed", exc.code)
        if attempt["cancel_requested"] or slot["state"] == "deleting":
            return self._conclude(ctx, attempt, compute, "cancelled", None)
        now = ctx.db.read(lambda uow: uow.now())
        if now >= attempt["deadline_at"]:
            return self._conclude(ctx, attempt, compute, "expired", "login_window_elapsed")
        adapter = self.adapters[slot["provider"]]
        if attempt["handle"] is None:
            spec = {
                "compute": compute,
                "compute_connection_id": slot["compute_connection_id"],
                "volume_name": slot["volume_name"],
                "mount": self.profile_mount,
                "env": dict(adapter.profile_env),
                "setup": adapter.setup_spec(attempt["mode"]),
                "tags": {
                    "sbx_workspace": slot["workspace_id"],
                    "sbx_slot": slot["id"],
                    "sbx_setup": attempt["id"],
                },
                "timeout": adapter.login_window_seconds + SETUP_VM_MARGIN_SECONDS,
            }
            with ctx.keepalive():
                found = self.backend.setup_start(spec, attempt["operation_id"])
            if found.get("status") == "terminated":
                return self._conclude(ctx, attempt, compute, "failed", _NEVER_STARTED)
            handle = {k: v for k, v in found.items() if k != "status"}
            ctx.commit(
                lambda uow: uow.update(
                    "slot_login_attempts",
                    attempt_id,
                    {"handle": handle, "updated_at": uow.now()},
                    expect={"state": list(LIVE_ATTEMPT_STATES)},
                )
            )
            return Continue(delay=self.poll_seconds / 2)
        observed = self.backend.setup_observe(attempt["handle"], compute, SETUP_STATE_PATH)
        state = observed.get("state") or {}
        phase = state.get("phase")
        if phase in ("succeeded", "failed", "expired"):
            return self._conclude(ctx, attempt, compute, phase, state.get("error"), state)
        if observed["status"] == "terminated":
            return self._conclude(ctx, attempt, compute, "failed", _NEVER_STARTED)
        progress: dict[str, Any] = {}
        if phase == "awaiting_user" and attempt["state"] == "starting":
            progress = {
                "state": "awaiting_user",
                "verification_url": str(state.get("verification_url") or "")[:500],
                "user_code": str(state.get("user_code") or "")[:40],
                "code_expires_at": _timestamp(state.get("code_expires_at")),
            }
        elif phase == "verifying" and attempt["state"] != "verifying":
            progress = {"state": "verifying", "user_code": None}
        if progress:
            ctx.commit(
                lambda uow: uow.update(
                    "slot_login_attempts",
                    attempt_id,
                    {**progress, "updated_at": uow.now()},
                    expect={"state": list(LIVE_ATTEMPT_STATES)},
                )
            )
        return Continue(delay=self.poll_seconds)

    def _conclude(
        self,
        ctx: Any,
        attempt: dict[str, Any],
        compute: dict[str, Any] | None,
        outcome: str,
        error: str | None,
        state: dict[str, Any] | None = None,
    ) -> Outcome:
        """Stop the Setup VM (confirmed), then record the outcome and free the Slot."""
        if compute is not None:
            with ctx.keepalive():
                handle = attempt["handle"]
                if handle is None:
                    # A create may have been requested before a crash: resolve it by identity.
                    found = self.backend.lookup(attempt["operation_id"], compute)
                    handle = found if found and found.get("status") == "running" else None
                if handle is not None and not self.backend.terminate(
                    handle, f"{attempt['operation_id']}:terminate", compute
                ):
                    return Retry("executor_unavailable", "setup VM termination is not confirmed")

        def commit(uow: Any) -> str:
            current = uow.get("slot_login_attempts", attempt["id"], lock=True)
            if current["state"] not in LIVE_ATTEMPT_STATES:
                return current["state"]
            now = uow.now()
            uow.update(
                "slot_login_attempts",
                attempt["id"],
                {
                    "state": outcome,
                    "user_code": None,
                    "error_code": (error or None) and str(error)[:120],
                    "finished_at": now,
                    "updated_at": now,
                },
            )
            slot = uow.get("machine_slots", attempt["slot_id"], lock=True)
            values: dict[str, Any] = {"updated_at": now}
            if slot["holder_kind"] == "setup" and slot["holder_id"] == attempt["id"]:
                values.update(holder_kind=None, holder_id=None)
            if slot["state"] != "deleting":
                if outcome == "succeeded":
                    values.update(
                        state="ready",
                        state_reason=(state or {}).get("warning"),
                        verified_at=now,
                        capabilities={
                            **(slot["capabilities"] or {}),
                            "cli_version": (state or {}).get("cli_version"),
                            "verification": {
                                "real_model_call": bool((state or {}).get("real_model_call")),
                                "observed_at": now.isoformat(),
                            },
                        },
                    )
                elif slot["state"] == "ready" and attempt["mode"] == "login":
                    # A re-login that did not complete leaves the previous login in place.
                    values.update(state_reason=f"relogin_{outcome}")
                elif (
                    error in (_NEVER_STARTED, "profile_sync_failed")
                    or outcome == "failed"
                    and (error or "").startswith(("executor_", "connection_", "credential_"))
                ):
                    values.update(state="error", state_reason=error)
                else:
                    values.update(state="needs_login", state_reason=error or outcome)
            slot = uow.update("machine_slots", slot["id"], values, bump_version=True)
            self._audit(uow, "system", "machine_slot.login_result", slot, result=outcome)
            return outcome

        return Succeeded({"state": ctx.commit(commit)})

    def handle_delete(self, ctx: Any) -> Outcome:
        slot_id = ctx.claim.job["machine_slot_id"]
        mode = ctx.input.get("mode", "delete")
        slot = ctx.db.read(lambda uow: uow.get("machine_slots", slot_id))
        if slot["state"] != "deleting":
            return Succeeded({"state": slot["state"]})
        attempt = ctx.db.read(
            lambda uow: (
                uow.get("slot_login_attempts", slot["current_login_attempt_id"])
                if slot["current_login_attempt_id"]
                else None
            )
        )
        if attempt is not None and attempt["state"] in LIVE_ATTEMPT_STATES:
            # The login Job observes the cancel request and stops its Setup VM first.
            return Continue(delay=self.poll_seconds / 2)
        if slot["volume_managed"]:
            compute = self.compute(slot["compute_connection_id"])
            with ctx.keepalive():
                self.backend.volume_delete(compute, slot["volume_name"])

        def commit(uow: Any) -> str:
            current = uow.get("machine_slots", slot_id, lock=True)
            if current["state"] != "deleting":
                return current["state"]
            now = uow.now()
            values: dict[str, Any] = {
                "holder_kind": None,
                "holder_id": None,
                "verified_at": None,
                "capabilities": {},
                "updated_at": now,
            }
            if mode == "logout":
                values.update(state="needs_login", state_reason="logged_out")
            else:
                values.update(state="deleted", state_reason=None, deleted_at=now)
            current = uow.update("machine_slots", slot_id, values, bump_version=True)
            self._audit(uow, "system", f"machine_slot.{mode}_result", current)
            return current["state"]

        return Succeeded({"state": ctx.commit(commit), "volume_deleted": slot["volume_managed"]})
