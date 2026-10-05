"""Vault-backed CredentialBroker: plaintext only at the effect boundary.

Selection considers exactly the Session's pinned, owner-scoped Connections.
There is no fallback to environment variables, host files, other principals'
accounts or operator credentials (RFC 06; A18/A19).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from protocol.capabilities import INFERENCE_CONNECTION_KIND

from control.application.ports import SealedRef
from control.domain.errors import DomainError


class VaultCredentialBroker:
    def __init__(self, tx: Any, vault: Any) -> None:
        self.tx = tx
        self.vault = vault

    def _connection(
        self, uow: Any, session: dict[str, Any], slot: str, kind: str, *, purpose: str
    ) -> dict[str, Any]:
        con_id = session.get(f"{slot}_connection_id")
        if not con_id:
            raise DomainError(
                "connection_required",
                f"a {kind} Connection is required",
                details={"kind": kind, "slot": slot},
                action="add_connection",
            )
        con = uow.get("connections", con_id)
        if con is None or con["workspace_id"] != session["workspace_id"] or con["kind"] != kind:
            raise DomainError(
                "connection_required",
                f"pinned {kind} Connection is unavailable",
                details={"kind": kind},
            )
        if con["config_state"] != "configured":
            raise DomainError(
                "connection_revoked",
                f"{kind} Connection is {con['config_state']}",
                details={"connection_id": con_id},
            )
        if purpose != "teardown" and con["health"] == "reauth_required":
            raise DomainError(
                "credential_invalid",
                f"{kind} credential needs replacement",
                details={"connection_id": con_id},
                action="replace_credential",
            )
        return con

    def _decrypt(
        self, slot: str, kind: str, session: dict[str, Any], purpose: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        def fn(uow: Any) -> tuple[dict[str, Any], dict[str, Any]]:
            current = uow.get("sessions", session["id"])
            con = self._connection(uow, current, slot, kind, purpose=purpose)
            version = uow.get("credential_versions", con["current_credential_version_id"])
            if version is None or version["revoked_at"] is not None:
                raise DomainError("connection_revoked", "credential version revoked")
            return con, version

        con, version = self.tx.read(fn)
        aad = self.vault.aad(con["workspace_id"], con["id"], version["id"], version["format"])
        material = self.vault.open(
            SealedRef(version["key_id"], version["nonce"], version["ciphertext"]), aad
        )
        return material, {"connection_id": con["id"], "credential_version_id": version["id"]}

    def check_inference(self, uow: Any, session: dict[str, Any]) -> dict[str, Any]:
        kind = INFERENCE_CONNECTION_KIND.get(session["harness_provider"])
        if kind is None:
            return {}
        con = self._connection(uow, session, "inference", kind, purpose="inference")
        return {
            "connection_id": con["id"],
            "credential_version_id": con["current_credential_version_id"],
        }

    def inference(self, session: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        kind = INFERENCE_CONNECTION_KIND.get(session["harness_provider"])
        if kind is None:
            return {}, {}
        material, meta = self._decrypt("inference", kind, session, "inference")
        return {kind: material}, meta

    def compute(self, session: dict[str, Any]) -> dict[str, Any] | None:
        if session["executor_backend"] != "modal":
            return None
        # Teardown authority is retained for live leases; disconnect is refused meanwhile.
        material, meta = self._decrypt("compute", "modal", session, "teardown")
        return {**material, **meta}

    def source(self, session: dict[str, Any]) -> dict[str, Any] | None:
        if not session.get("source_connection_id"):
            return None
        material, _ = self._decrypt("source", "github", session, "source_control")
        return {"username": "x-access-token", "password": material["token"]}

    def source_token(self, workspace_id: str, connection_id: str) -> tuple[str, dict[str, Any]]:
        """Delivery boundary: resolve the selected GitHub Connection under current authority."""
        session_like = {
            "id": None,
            "workspace_id": workspace_id,
            "source_connection_id": connection_id,
        }

        def fn(uow: Any) -> tuple[dict[str, Any], dict[str, Any]]:
            con = self._connection(uow, session_like, "source", "github", purpose="source_control")
            return con, uow.get("credential_versions", con["current_credential_version_id"])

        con, version = self.tx.read(fn)
        if version is None or version["revoked_at"] is not None:
            raise DomainError("connection_revoked", "credential version revoked")
        material = self.vault.open(
            SealedRef(version["key_id"], version["nonce"], version["ciphertext"]),
            self.vault.aad(con["workspace_id"], con["id"], version["id"], version["format"]),
        )
        return material["token"], {
            "connection_id": con["id"],
            "credential_version_id": version["id"],
        }

    def report_health(
        self, uow: Any, connection_id: str | None, credential_version_id: str | None, health: str
    ) -> None:
        if not connection_id:
            return
        con = uow.get("connections", connection_id, lock=True)
        if (
            con is None
            or con["current_credential_version_id"] != credential_version_id
            or con["config_state"] != "configured"
        ):
            return  # observation about a replaced/revoked version is discarded
        if health == "invalid":
            uow.update(
                "connections",
                connection_id,
                {
                    "health": "reauth_required",
                    "health_reason": "provider_rejected_credential",
                    "updated_at": uow.now(),
                },
            )
        elif health == "rate_limited":
            uow.update(
                "connections",
                connection_id,
                {
                    "health": "degraded",
                    "health_reason": "rate_limited",
                    "cooldown_until": uow.now() + timedelta(seconds=60),
                },
            )
