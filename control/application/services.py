"""Services application: desired state per Session across leases; Job-realized (RFC 03)."""

from __future__ import annotations

from typing import Any

from control.application import access
from control.application.ports import RuntimeConnector, RuntimeRefused, RuntimeUnavailable
from control.domain.digests import digest_of
from control.domain.errors import DomainError
from control.domain.identity import Principal
from control.domain.ids import new_id
from control.jobs.model import Outcome, Retry, Succeeded


class Services:
    def __init__(self, tx: Any, connector: RuntimeConnector) -> None:
        self.tx = tx
        self.connector = connector

    def list(self, principal: Principal, session_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            session = access.owned(uow, principal, "sessions", session_id, what="session")
            desires = {
                d["name"]: d for d in uow.find("service_desires", {"session_id": session_id})
            }
            lease = uow.find_one("executor_leases", {"session_id": session_id, "state": "ready"})
            instances = (
                {
                    i["name"]: i
                    for i in uow.find(
                        "service_instances", {"session_id": session_id, "lease_id": lease["id"]}
                    )
                }
                if lease
                else {}
            )
            items = []
            for decl in session["effective_spec"].get("services") or []:
                d, inst = desires.get(decl["name"]), instances.get(decl["name"])
                items.append(
                    {
                        "name": decl["name"],
                        "port": decl.get("port"),
                        "preview": decl.get("preview", False),
                        "desired": d["desired"] if d else "stopped",
                        "state": inst["state"] if inst else ("offline" if not lease else "stopped"),
                        "observed_at": inst["observed_at"].isoformat() if inst else None,
                        "lease_live": lease is not None,
                    }
                )
            return {"items": items, "event_watermark": uow.watermark(session_id)}

        return self.tx.read(fn)

    def set_desired(
        self, principal: Principal, session_id: str, name: str, desired: str
    ) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            session = access.owned(
                uow, principal, "sessions", session_id, lock=True, what="session"
            )
            decl = next(
                (s for s in session["effective_spec"].get("services") or [] if s["name"] == name),
                None,
            )
            if decl is None:
                raise DomainError(
                    "not_found", "service is not declared by this Session's ProjectVersion"
                )
            row = uow.find_one(
                "service_desires", {"session_id": session_id, "name": name}, lock=True
            )
            if row is None:
                row = uow.insert(
                    "service_desires",
                    {
                        "id": new_id("service_desire"),
                        "workspace_id": session["workspace_id"],
                        "session_id": session_id,
                        "name": name,
                        "declaration": decl,
                        "declaration_digest": digest_of(decl),
                        "desired": desired,
                    },
                )
            else:
                row = uow.update(
                    "service_desires",
                    row["id"],
                    {"desired": desired, "updated_at": uow.now()},
                    bump_version=True,
                )
            uow.append_event(
                session,
                "service.requested" if desired == "running" else "service.stopped",
                {"name": name, "desired": desired, "declaration_digest": row["declaration_digest"]},
                actor=principal.user_id,
            )
            kind = "service.ensure" if desired == "running" else "service.stop"
            job = uow.enqueue_job(
                workspace_id=session["workspace_id"],
                kind=kind,
                target_id=row["id"],
                dedupe_key=f"{row['id']}:{row['version']}",
                session_id=session_id,
            )
            return {"name": name, "desired": desired, "job_id": job}

        return self.tx.run(fn)

    def logs(self, principal: Principal, session_id: str, name: str) -> dict[str, Any]:
        lease = self.tx.read(
            lambda uow: (
                access.owned(uow, principal, "sessions", session_id, what="session"),
                uow.find_one("executor_leases", {"session_id": session_id, "state": "ready"}),
            )[1]
        )
        if lease is None:
            raise DomainError("executor_unavailable", "service logs need a live executor")
        try:
            return self.connector.channel(lease).query("service.logs", name=name)
        except (RuntimeUnavailable, RuntimeRefused) as exc:
            raise DomainError("executor_unavailable", str(exc)) from exc

    def handle(self, ctx: Any) -> Outcome:
        desire_id = ctx.claim.job["service_desire_id"]

        def read(uow: Any) -> tuple[Any, Any]:
            desire = uow.get("service_desires", desire_id)
            return desire, uow.find_one(
                "executor_leases", {"session_id": desire["session_id"], "state": "ready"}
            )

        desire, lease = ctx.db.read(read)
        if lease is None:
            return Succeeded(
                {"deferred": "no live lease; desired state applies on next activation"}
            )
        try:
            channel = self.connector.channel(lease)
            if desire["desired"] == "running":
                response = channel.op(
                    "service.ensure",
                    f"{desire_id}:{desire['version']}:{lease['id']}",
                    desire["session_id"],
                    {"declaration": desire["declaration"]},
                )
            else:
                response = channel.op(
                    "service.stop",
                    f"{desire_id}:{desire['version']}:{lease['id']}:stop",
                    desire["session_id"],
                    {"name": desire["name"]},
                )
        except RuntimeUnavailable as exc:
            return Retry("executor_unavailable", str(exc)[:200])
        observed = response.get("result") or {}
        state = observed.get("state") if response.get("status") == "succeeded" else "failed"
        state = (
            state if state in ("starting", "ready", "degraded", "stopped", "failed") else "failed"
        )
        ctx.commit(lambda uow: self._observe(uow, desire, lease, state, observed))
        return Succeeded({"state": state})

    def _observe(
        self,
        uow: Any,
        desire: dict[str, Any],
        lease: dict[str, Any],
        state: str,
        observed: dict[str, Any],
    ) -> None:
        key = {"session_id": desire["session_id"], "name": desire["name"], "lease_id": lease["id"]}
        row = uow.find_one("service_instances", key, lock=True)
        if row is None:
            uow.insert(
                "service_instances",
                {
                    "id": new_id("service_instance"),
                    "workspace_id": desire["workspace_id"],
                    **key,
                    "lease_generation": lease["generation"],
                    "state": state,
                    "observed": observed,
                },
            )
        else:
            uow.update(
                "service_instances",
                row["id"],
                {"state": state, "observed": observed, "observed_at": uow.now()},
            )
        session = uow.get("sessions", desire["session_id"], lock=True)
        event = {
            "ready": "service.ready",
            "starting": "service.ready",
            "degraded": "service.degraded",
            "stopped": "service.stopped",
        }.get(state, "service.failed")
        uow.append_event(
            session,
            event,
            {"name": desire["name"], "state": state, "lease_id": lease["id"]},
            actor="application",
            executor_lease_id=lease["id"],
            lease_generation=lease["generation"],
        )

    def ensure_on_activation(
        self, ctx: Any, lease: dict[str, Any], session: dict[str, Any]
    ) -> None:
        """Desired state persists across leases: re-realize running services on a new lease."""

        def fn(uow: Any) -> None:
            for desire in uow.find(
                "service_desires", {"session_id": session["id"], "desired": "running"}
            ):
                uow.enqueue_job(
                    workspace_id=desire["workspace_id"],
                    kind="service.ensure",
                    target_id=desire["id"],
                    dedupe_key=f"{desire['id']}:{lease['id']}",
                    session_id=session["id"],
                )

        ctx.commit(fn)
