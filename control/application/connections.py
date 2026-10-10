"""Connection application + vault boundary (RFC 06 One Connection domain).

Single writer for Connection metadata and CredentialVersion ciphertext. Secrets
are write-only: no view ever includes plaintext, fingerprints or ciphertext.
Validation runs as a Job tied to the exact credential version and commits only
if (version, credential version, revocation epoch) are still current.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from protocol.capabilities import (
    INFERENCE_KIND,
    REASONING_OFF,
    REASONING_TOGGLE_PROTOCOLS,
    select_endpoint,
)

from control.application import access
from control.domain.errors import DomainError
from control.domain.identity import Principal
from control.domain.ids import new_id
from control.jobs.model import Continue, Outcome, Retry, Succeeded
from control.security.redaction import safe_traceback

log = logging.getLogger("sbx.connections")

KINDS = ("modal", "github", "inference_api")
# Vendor-specific inference kinds are retired: stored rows stay usable, new ones are refused.
LEGACY_KINDS = ("opencode_zen", "codex")
SLOT_DEFAULTS = {"modal": 4, "github": 8, "inference_api": 4}


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


class Connections:
    def __init__(
        self,
        tx: Any,
        vault: Any,
        connectors: dict[str, Any],
        *,
        validators: dict[str, Any] | None = None,
        provisioners: dict[str, Any] | None = None,
    ) -> None:
        self.tx = tx
        self.vault = vault
        self.connectors = connectors
        # kind -> callable(plaintext, **ctx) -> Observation; defaults to connector.validate
        self.validators = validators or {}
        # kind -> callable(plaintext, connection_id) -> safe result, run after validation
        self.provisioners = provisioners or {}
        # Other applications register dependents (e.g. executing Deliveries) here.
        self.dependency_hooks: list[Any] = []

    # ----------------------------------------------------------------------- views
    def view(self, uow: Any, con: dict[str, Any]) -> dict[str, Any]:
        version = (
            uow.get("credential_versions", con["current_credential_version_id"])
            if con["current_credential_version_id"]
            else None
        )
        latest = {}
        if version is not None:
            latest = {
                o["kind"]: o
                for o in uow.query(
                    "observations.latest",
                    connection_id=con["id"],
                    credential_version_id=version["id"],
                )
            }
        validation = latest.get("validation")
        catalog = latest.get("catalog")
        return {
            "id": con["id"],
            "workspace_id": con["workspace_id"],
            "kind": con["kind"],
            "label": con["label"],
            "acquisition": con["acquisition"],
            "state": con["config_state"],
            "health": con["health"],
            "health_reason": con["health_reason"],
            "external_identity": con["external_identity"],
            "version": con["version"],
            "slot_limit": con["slot_limit"],
            "credential": {
                "id": version["id"],
                "ordinal": version["ordinal"],
                "format": version["format"],
                "created_at": _iso(version["created_at"]),
            }
            if version and con["config_state"] != "revoked"
            else None,
            # Non-secret settings of the current CredentialVersion (never key material).
            "config": (version or {}).get("public_config") or {},
            "legacy": con["kind"] in LEGACY_KINDS,
            "validation": {
                "status": validation["status"],
                "observed_at": _iso(validation["observed_at"]),
                "details": validation["safe_details"],
                "quota_consuming": validation["quota_consuming"],
            }
            if validation
            else None,
            "catalog": {"observed_at": _iso(catalog["observed_at"]), **catalog["safe_details"]}
            if catalog
            else None,
            "created_at": _iso(con["created_at"]),
            "updated_at": _iso(con["updated_at"]),
            "revoked_at": _iso(con["revoked_at"]),
        }

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
        kind = body.get("kind")
        if kind in LEGACY_KINDS:
            raise DomainError(
                "validation_failed",
                f"{kind} connections are retired; add an inference_api connection instead",
                details={"field": "kind", "replacement": INFERENCE_KIND},
            )
        if kind not in KINDS:
            raise DomainError(
                "validation_failed", "unknown connection kind", details={"field": "kind"}
            )
        material = self.connectors[kind].normalize(body.get("credential") or {})
        label = str(body.get("label") or kind).strip()[:80]
        request = {
            "kind": kind,
            "label": label,
            "material_fingerprint": self.vault.fingerprint(str(sorted(material.items()))),
        }

        def fn(uow: Any) -> dict[str, Any]:
            replay = uow.dedupe_begin(
                principal_id=principal.user_id,
                workspace_id=workspace_id,
                command_kind="connections.create",
                key=idempotency_key,
                request=request,
            )
            if replay is not None:
                return replay
            con = uow.insert(
                "connections",
                {
                    "id": new_id("connection"),
                    "workspace_id": workspace_id,
                    "kind": kind,
                    "label": label,
                    "created_by": principal.user_id,
                    "slot_limit": int(body.get("slot_limit") or SLOT_DEFAULTS[kind]),
                },
            )
            version = self._seal_version(uow, principal, con, material)
            con = uow.update(
                "connections",
                con["id"],
                {"current_credential_version_id": version["id"], "health": "verifying"},
            )
            uow.enqueue_job(
                workspace_id=workspace_id,
                kind="connection.validate",
                target_id=con["id"],
                dedupe_key=f"{con['id']}:{version['id']}",
                input={"credential_version_id": version["id"]},
            )
            uow.audit(
                actor=principal.user_id,
                action="connection.create",
                purpose=kind,
                target_kind="connection",
                target_id=con["id"],
                target_version=con["version"],
                result="ok",
                workspace_id=workspace_id,
            )
            response = self.view(uow, con)
            uow.dedupe_finish(
                principal_id=principal.user_id,
                workspace_id=workspace_id,
                command_kind="connections.create",
                key=idempotency_key,
                response=response,
            )
            return response

        return self.tx.run(fn)

    def _seal_version(
        self, uow: Any, principal: Principal, con: dict[str, Any], material: dict[str, Any]
    ) -> dict[str, Any]:
        connector = self.connectors[con["kind"]]
        version_id = new_id("credential_version")
        ordinal = uow.count("credential_versions", {"connection_id": con["id"]}) + 1
        sealed = self.vault.seal(
            material, self.vault.aad(con["workspace_id"], con["id"], version_id, connector.FORMAT)
        )
        return uow.insert(
            "credential_versions",
            {
                "id": version_id,
                "workspace_id": con["workspace_id"],
                "connection_id": con["id"],
                "ordinal": ordinal,
                "format": connector.FORMAT,
                "key_id": sealed.key_id,
                "nonce": sealed.nonce,
                "ciphertext": sealed.ciphertext,
                "fingerprint": self.vault.fingerprint(str(sorted(material.items()))),
                "public_config": getattr(connector, "public_config", lambda _m: {})(material),
                "created_by": principal.user_id,
            },
        )

    def replace(
        self,
        principal: Principal,
        connection_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            con = access.owned(
                uow, principal, "connections", connection_id, lock=True, what="connection"
            )
            if con["kind"] in LEGACY_KINDS:
                raise DomainError(
                    "validation_failed",
                    f"{con['kind']} connections are retired; add an inference_api connection",
                    details={"field": "kind", "replacement": INFERENCE_KIND},
                )
            submitted = dict(body.get("credential") or {})
            if hasattr(self.connectors[con["kind"]], "public_config"):
                # Rotating only the key keeps the current endpoints and model.
                current = uow.get("credential_versions", con["current_credential_version_id"])
                kept = dict((current or {}).get("public_config") or {})
                if "base_url" in submitted:
                    kept.pop("endpoints", None)
                if "model" in submitted:
                    kept.pop("models", None)
                submitted = {**kept, **submitted}
            material = self.connectors[con["kind"]].normalize(submitted)
            request = {
                "expected_version": body.get("expected_version"),
                "material_fingerprint": self.vault.fingerprint(str(sorted(material.items()))),
            }
            replay = uow.dedupe_begin(
                principal_id=principal.user_id,
                workspace_id=con["workspace_id"],
                command_kind=f"connections.replace:{connection_id}",
                key=idempotency_key,
                request=request,
            )
            if replay is not None:
                return replay
            if con["config_state"] == "revoked":
                raise DomainError(
                    "connection_revoked",
                    "revoked connections cannot be re-enabled; create a new connection",
                )
            if (
                body.get("expected_version") is None
                or int(body["expected_version"]) != con["version"]
            ):
                raise DomainError(
                    "version_conflict",
                    "connection changed",
                    details={"current_version": con["version"]},
                )
            version = self._seal_version(uow, principal, con, material)
            con = uow.update(
                "connections",
                connection_id,
                {
                    "current_credential_version_id": version["id"],
                    "revocation_epoch": con["revocation_epoch"] + 1,
                    "health": "verifying",
                    "health_reason": None,
                    "external_identity": None,
                    "cooldown_until": None,
                    "updated_at": uow.now(),
                },
                bump_version=True,
            )
            uow.update_where(
                "credential_grants",
                {"connection_id": connection_id, "revoked_at": None},
                {"revoked_at": uow.now()},
            )
            uow.enqueue_job(
                workspace_id=con["workspace_id"],
                kind="connection.validate",
                target_id=connection_id,
                dedupe_key=f"{connection_id}:{version['id']}",
                input={"credential_version_id": version["id"]},
            )
            uow.audit(
                actor=principal.user_id,
                action="connection.replace",
                purpose=con["kind"],
                target_kind="connection",
                target_id=connection_id,
                target_version=con["version"],
                result="ok",
                workspace_id=con["workspace_id"],
            )
            response = self.view(uow, con)
            uow.dedupe_finish(
                principal_id=principal.user_id,
                workspace_id=con["workspace_id"],
                command_kind=f"connections.replace:{connection_id}",
                key=idempotency_key,
                response=response,
            )
            return response

        return self.tx.run(fn)

    def request_validation(self, principal: Principal, connection_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            con = access.owned(
                uow, principal, "connections", connection_id, lock=True, what="connection"
            )
            if con["config_state"] != "configured":
                raise DomainError("connection_revoked", "connection is not configured")
            version_id = con["current_credential_version_id"]
            uow.update("connections", connection_id, {"health": "verifying"})
            job = uow.enqueue_job(
                workspace_id=con["workspace_id"],
                kind="connection.validate",
                target_id=connection_id,
                dedupe_key=f"{connection_id}:{version_id}:manual",
                input={"credential_version_id": version_id},
            )
            return {
                "connection": self.view(uow, uow.get("connections", connection_id)),
                "job_id": job,
            }

        return self.tx.run(fn)

    def update(
        self, principal: Principal, connection_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            con = access.owned(
                uow, principal, "connections", connection_id, lock=True, what="connection"
            )
            if (
                body.get("expected_version") is None
                or int(body["expected_version"]) != con["version"]
            ):
                raise DomainError(
                    "version_conflict",
                    "connection changed",
                    details={"current_version": con["version"]},
                )
            values: dict[str, Any] = {"updated_at": uow.now()}
            if "label" in body:
                values["label"] = str(body["label"]).strip()[:80] or con["label"]
            if "priority" in body:
                values["priority"] = int(body["priority"])
            return self.view(
                uow, uow.update("connections", connection_id, values, bump_version=True)
            )

        return self.tx.run(fn)

    def disconnect(self, principal: Principal, connection_id: str) -> dict[str, Any]:
        """Revoke + tombstone. Refused while dependent compute/effects exist (no orphans)."""

        def fn(uow: Any) -> dict[str, Any]:
            con = access.owned(
                uow, principal, "connections", connection_id, lock=True, what="connection"
            )
            if con["config_state"] == "revoked":
                return self.view(uow, con)
            dependents = self._dependents(uow, con)
            if dependents:
                raise DomainError(
                    "connection_in_use",
                    "release dependent executors/effects before disconnecting; teardown authority is retained until then",
                    details=dependents,
                    action="release_dependents",
                )
            now = uow.now()
            con = uow.update(
                "connections",
                connection_id,
                {
                    "config_state": "revoked",
                    "revoked_at": now,
                    "revocation_epoch": con["revocation_epoch"] + 1,
                    "health": "reauth_required",
                    "health_reason": "revoked",
                    "updated_at": now,
                },
                bump_version=True,
            )
            uow.update_where(
                "credential_versions",
                {"connection_id": connection_id, "revoked_at": None},
                {"revoked_at": now},
            )
            uow.update_where(
                "credential_grants",
                {"connection_id": connection_id, "revoked_at": None},
                {"revoked_at": now},
            )
            uow.update_where(
                "jobs",
                {"connection_id": connection_id, "state": ["queued", "retry_wait"]},
                {"state": "cancelled", "finished_at": now, "last_error": "connection revoked"},
            )
            uow.audit(
                actor=principal.user_id,
                action="connection.revoke",
                purpose=con["kind"],
                target_kind="connection",
                target_id=connection_id,
                target_version=con["version"],
                result="ok",
                workspace_id=con["workspace_id"],
            )
            return {**self.view(uow, con), "provider_side_revocation": "not_performed"}

        return self.tx.run(fn)

    def _dependents(self, uow: Any, con: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if con["kind"] == "modal":
            leases = uow.query("leases.live_for_connection", connection_id=con["id"])
            if leases:
                out["executor_leases"] = [lz["id"] for lz in leases]
        live = uow.find(
            "executions",
            {
                "inference_connection_id": con["id"],
                "state": ["preparing", "started", "stop_requested"],
            },
        )
        if live:
            out["executions"] = [e["id"] for e in live]
        quarantined = uow.find(
            "capacity_reservations", {"connection_id": con["id"], "state": "quarantined"}
        )
        if quarantined:
            out["quarantined_reservations"] = [r["id"] for r in quarantined]
        for hook in self.dependency_hooks:
            out.update(hook(uow, con))
        return out

    # ------------------------------------------------------------------------ queries
    def get(self, principal: Principal, connection_id: str) -> dict[str, Any]:
        return self.tx.read(
            lambda uow: self.view(
                uow, access.owned(uow, principal, "connections", connection_id, what="connection")
            )
        )

    def list(
        self, principal: Principal, workspace_id: str, *, include_revoked: bool = False
    ) -> dict[str, Any]:
        access.require_workspace(principal, workspace_id)

        def fn(uow: Any) -> dict[str, Any]:
            where: dict[str, Any] = {"workspace_id": workspace_id}
            if not include_revoked:
                where["config_state"] = ["configured", "disabled"]
            return {
                "items": [
                    self.view(uow, c) for c in uow.find("connections", where, order="created_at")
                ]
            }

        return self.tx.read(fn)

    def capabilities(self, principal: Principal, connection_id: str) -> dict[str, Any]:
        view = self.get(principal, connection_id)
        return {
            "connection_id": connection_id,
            "health": view["health"],
            "validation": view["validation"],
            "catalog": view["catalog"],
        }

    def models(
        self,
        principal: Principal,
        workspace_id: str,
        *,
        provider_id: str = "opencode",
        accepted: Any = None,
    ) -> dict[str, Any]:
        """Connection-scoped model availability from the latest catalog observation (pure).

        ``accepted`` is the Harness's ``inference_protocols``; each Connection reports the
        endpoint that Harness would use, or ``compatible: false`` when it offers none.
        """
        listing = self.list(principal, workspace_id)
        out = []
        for con in listing["items"]:
            if con["kind"] != INFERENCE_KIND or con["state"] != "configured":
                continue
            catalog, config = con["catalog"] or {}, con["config"]
            endpoint = select_endpoint(config.get("endpoints"), accepted)
            out.append(
                {
                    "connection_id": con["id"],
                    "label": con["label"],
                    "health": con["health"],
                    "observed_at": catalog.get("observed_at"),
                    "preferred_model": config.get("model"),
                    # ``efforts``: reasoning values this Harness may send for the model;
                    # empty unless both the probe and the official CLI were verified.
                    "models": [
                        {
                            **m,
                            "efforts": [REASONING_OFF]
                            if endpoint
                            and endpoint[0] in REASONING_TOGGLE_PROTOCOLS.get(provider_id, ())
                            and (m.get("reasoning") or {}).get(endpoint[0]) == "toggle"
                            else [],
                        }
                        for m in catalog.get("models")
                        or [{"id": m} for m in config.get("models") or []]
                    ],
                    "protocols": list(config.get("endpoints") or {}),
                    "protocol": endpoint[0] if endpoint else None,
                    "compatible": endpoint is not None,
                }
            )
        preferred = next(
            (
                c["preferred_model"]
                for c in out
                if c["compatible"] and c["health"] != "reauth_required" and c["preferred_model"]
            ),
            None,
        )
        return {
            "provider_id": provider_id,
            "inference_protocols": list(accepted or ()),
            "preferred_model": preferred,
            "connections": out,
        }

    # --------------------------------------------------------------------- jobs/hooks
    def handle_validate(self, ctx: Any) -> Outcome:
        connection_id = ctx.claim.job["connection_id"]
        version_id = ctx.input.get("credential_version_id")

        def read(uow: Any) -> tuple[Any, Any, list[str]]:
            con = uow.get("connections", connection_id)
            version = uow.get("credential_versions", version_id) if version_id else None
            repos = []
            if con["kind"] == "github":
                repos = sorted(
                    {
                        f"{v['repo_owner']}/{v['repo_name']}"
                        for v in uow.find("project_versions", {"workspace_id": con["workspace_id"]})
                        if v["repo_owner"]
                    }
                )[:20]
            return con, version, repos

        con, version, repos = ctx.db.read(read)
        if (
            con["config_state"] != "configured"
            or version is None
            or version["revoked_at"]
            or con["current_credential_version_id"] != version_id
        ):
            return Succeeded({"skipped": "stale_or_revoked"})
        material = self.decrypt(version, con)
        validate = self.validators.get(con["kind"]) or self.connectors[con["kind"]].validate
        kwargs = {"repositories": repos} if con["kind"] == "github" else {}
        try:
            observation = validate(material, **kwargs)
        except Exception as exc:
            # Connector exception text can embed the credential under test: surface only
            # its type; the redacted traceback goes to the log.
            log.warning("validator for %s failed\n%s", con["kind"], safe_traceback(exc))
            return Retry("validation_unavailable", f"validator raised {type(exc).__name__}")
        expected_epoch, expected_version = con["revocation_epoch"], con["version"]

        def commit(uow: Any) -> str:
            current = uow.get("connections", connection_id, lock=True)
            # CAS: a replacement/revocation that committed meanwhile wins (A19).
            if (
                current["current_credential_version_id"],
                current["revocation_epoch"],
                current["version"],
                current["config_state"],
            ) != (version_id, expected_epoch, expected_version, "configured"):
                return "stale"
            now = uow.now()
            uow.insert(
                "connection_observations",
                {
                    "id": new_id("observation"),
                    "workspace_id": current["workspace_id"],
                    "connection_id": connection_id,
                    "credential_version_id": version_id,
                    "kind": "validation",
                    "status": observation.status,
                    "connection_version": current["version"],
                    "safe_details": observation.details,
                    "quota_consuming": observation.quota_consuming,
                    "expires_at": now + timedelta(hours=24),
                },
            )
            if observation.catalog is not None:
                uow.insert(
                    "connection_observations",
                    {
                        "id": new_id("observation"),
                        "workspace_id": current["workspace_id"],
                        "connection_id": connection_id,
                        "credential_version_id": version_id,
                        "kind": "catalog",
                        "status": "ready",
                        "connection_version": current["version"],
                        "safe_details": observation.catalog,
                        "expires_at": now + timedelta(hours=24),
                    },
                )
            health = {
                "ready": "ready",
                "invalid": "reauth_required",
                "degraded": "degraded",
                "error": "unverified",
            }[observation.status]
            uow.update(
                "connections",
                connection_id,
                {
                    "health": health,
                    "health_reason": (observation.details or {}).get("reason"),
                    "external_identity": observation.external_identity
                    or current["external_identity"],
                    "updated_at": now,
                },
            )
            uow.audit(
                actor="system",
                action="connection.validate",
                purpose=current["kind"],
                target_kind="connection",
                target_id=connection_id,
                target_version=current["version"],
                result=observation.status,
                workspace_id=current["workspace_id"],
            )
            if observation.status == "ready" and current["kind"] in self.provisioners:
                # Build owner-side resources ahead of first use (Modal: the runtime image).
                uow.enqueue_job(
                    workspace_id=current["workspace_id"],
                    kind="connection.provision",
                    target_id=connection_id,
                    dedupe_key=f"{connection_id}:{version_id}",
                    input={"credential_version_id": version_id},
                    max_attempts=5,
                )
            return observation.status

        result = ctx.commit(commit)
        if observation.status == "error" and result != "stale":
            return Retry(
                "validation_unavailable",
                str(observation.details.get("reason")),
                retry_after=observation.retry_after,
            )
        if observation.status == "degraded" and observation.retry_after:
            return Continue(delay=observation.retry_after)
        return Succeeded({"status": result})

    def handle_provision(self, ctx: Any) -> Outcome:
        """Best-effort owner-side preparation; first use still resolves everything itself."""
        connection_id = ctx.claim.job["connection_id"]
        version_id = ctx.input.get("credential_version_id")
        con, version = ctx.db.read(
            lambda uow: (
                uow.get("connections", connection_id),
                uow.get("credential_versions", version_id) if version_id else None,
            )
        )
        provision = self.provisioners.get(con["kind"])
        if (
            provision is None
            or con["config_state"] != "configured"
            or version is None
            or version["revoked_at"]
            or con["current_credential_version_id"] != version_id
        ):
            return Succeeded({"skipped": "stale_or_revoked"})
        material = self.decrypt(version, con)
        with ctx.keepalive():
            result = provision(material, connection_id)
        return Succeeded(result)

    def material_for(self, connection_id: str, kind: str) -> dict[str, Any]:
        """Current credential of a usable Connection, for a worker-side effect only."""

        def read(uow: Any) -> tuple[Any, Any]:
            con = uow.get("connections", connection_id)
            version = (
                uow.get("credential_versions", con["current_credential_version_id"])
                if con and con["current_credential_version_id"]
                else None
            )
            return con, version

        con, version = self.tx.read(read)
        if (
            con is None
            or con["kind"] != kind
            or con["config_state"] != "configured"
            or version is None
            or version["revoked_at"]
        ):
            raise DomainError(
                "connection_required",
                f"the {kind} connection is not usable",
                details={"kind": kind},
            )
        return self.decrypt(version, con)

    def decrypt(self, version: dict[str, Any], con: dict[str, Any]) -> dict[str, Any]:
        from control.application.ports import SealedRef

        return self.vault.open(
            SealedRef(version["key_id"], version["nonce"], version["ciphertext"]),
            self.vault.aad(con["workspace_id"], con["id"], version["id"], version["format"]),
        )
