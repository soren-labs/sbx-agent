"""Connection application: one Connection domain + encrypted
CredentialVersions + purpose-bound CredentialGrants (RFC 167 §06).

Invariants enforced here:
- Plaintext enters once via a write-only create/replace call; only
  ciphertext + safe fingerprint ever persist.
- AAD binds workspace/connection/version/format — ciphertext cannot be
  replayed under another owner.
- Replace is atomic: new CredentialVersion + CAS pointer + revocation
  epoch bump + observation/grant invalidation + validation job.
- Disconnect keeps a tombstone and is rejected while nonterminal
  executions depend on its grants (no orphaned Modal compute).
- Materialization only ever happens for an explicit configured
  connection in the caller's workspace — no ambient/global fallback.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from control.application.sessions import enqueue_job
from control.domain.connections import (
    AcquisitionMethod,
    ConnectionKind,
    ConnectionState,
    validate_manual_payload,
)
from control.domain.errors import DomainError
from control.domain.ids import new_id
from control.domain.jobs import JobKind, TargetFamily
from control.persistence.unit_of_work import SqlUnitOfWork
from control.security.vault import Vault, credential_aad

# Purposes: where plaintext may be materialized (RFC destination table).
PURPOSE_EXECUTOR_WORKER = "executor_worker"  # modal token → executor backend
PURPOSE_DELIVERY_WORKER = "delivery_worker"  # github token → delivery
PURPOSE_RUNTIME_EXECUTION = "runtime_execution"  # zen/codex → runtime HOME
PURPOSE_GIT_CLONE = "git_clone"  # github → clone helper

_ALLOWED_PURPOSES: dict[ConnectionKind, frozenset[str]] = {
    ConnectionKind.MODAL: frozenset({PURPOSE_EXECUTOR_WORKER}),
    ConnectionKind.GITHUB: frozenset({PURPOSE_DELIVERY_WORKER, PURPOSE_GIT_CLONE}),
    ConnectionKind.OPENCODE_ZEN: frozenset({PURPOSE_RUNTIME_EXECUTION}),
    ConnectionKind.CODEX: frozenset({PURPOSE_RUNTIME_EXECUTION}),
}

# Safe fingerprint fields persisted (never secret): what the UI/logs may show.
_FINGERPRINT_FIELDS: dict[str, tuple[str, ...]] = {
    "token_pair": ("token_id_prefix",),
    "personal_token": ("token_prefix",),
    "api_key": ("key_prefix",),
    "auth_bundle": ("file_names",),
}


def _fingerprint(fmt: str, payload: dict) -> dict:
    """Non-secret metadata only. Prefix is capped at 4 chars."""
    fp: dict = {}
    if fmt == "token_pair":
        fp["token_id_prefix"] = payload["token_id"][:4]
    elif fmt == "personal_token":
        fp["token_prefix"] = payload["token"][:4]
    elif fmt == "api_key":
        fp["key_prefix"] = payload["api_key"][:4]
    elif fmt == "auth_bundle":
        files = payload.get("files") or {}
        fp["file_names"] = sorted(files)
    fp["sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    return fp


@dataclass
class MaterializedCredential:
    """Private plaintext bundle — never crosses an event/log boundary."""

    connection_id: str
    credential_version_id: str
    format: str
    payload: dict
    grant_id: str | None = None


class ConnectionService:
    def __init__(self, db, vault: Vault):
        self.db = db
        self.vault = vault

    # -- lifecycle --------------------------------------------------------

    def create_connection(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        principal_id: str,
        kind: str,
        label: str | None,
        credential: dict,  # {"format": ..., "payload": {...}} — write-only
        acquisition: str = "manual",
        allowed_purposes: list[str] | None = None,
    ) -> dict:
        kind_e = ConnectionKind(kind)
        fmt = credential["format"]
        payload = credential["payload"]
        validate_manual_payload(kind_e, fmt, payload)
        purposes = allowed_purposes or sorted(_ALLOWED_PURPOSES[kind_e])
        for p in purposes:
            if p not in _ALLOWED_PURPOSES[kind_e]:
                raise DomainError("validation_failed", f"purpose {p!r} not allowed for {kind}")

        connection_id = new_id("connection")
        cred_id = new_id("credential_version")
        aad = credential_aad(
            workspace_id=workspace_id,
            connection_id=connection_id,
            credential_version_id=cred_id,
            fmt=fmt,
        )
        ct, key_id, nonce = self.vault.seal(json.dumps(payload).encode(), aad=aad)

        uow.connections.insert(
            {
                "id": connection_id,
                "workspace_id": workspace_id,
                "kind": kind_e.value,
                "label": label,
                "created_by": principal_id,
                "allowed_principals": [principal_id],
                "allowed_purposes": purposes,
                "acquisition": AcquisitionMethod(acquisition).value,
                "state": ConnectionState.CONFIGURED.value,
                "health": "unverified",
                "current_credential_version_id": None,
            }
        )
        uow.credential_versions.insert(
            {
                "id": cred_id,
                "workspace_id": workspace_id,
                "connection_id": connection_id,
                "ordinal": 1,
                "state": "active",
                "format": fmt,
                "ciphertext": ct,
                "key_id": key_id,
                "nonce": nonce,
                "aad": aad,
                "fingerprint": _fingerprint(fmt, payload),
            }
        )
        uow.conn.execute(
            "UPDATE connections SET current_credential_version_id=%s WHERE id=%s",
            (cred_id, connection_id),
        )
        job = enqueue_job(
            uow,
            workspace_id=workspace_id,
            kind=JobKind.CONNECTION_VALIDATE,
            target_family=TargetFamily.CONNECTION,
            target_id=connection_id,
            dedupe_key=f"connection.validate:{connection_id}:{cred_id}",
            payload={
                "connection_id": connection_id,
                "credential_version_id": cred_id,
            },
        )
        uow.commit()
        conn = uow.connections.get(workspace_id, connection_id)
        conn["validation_job_id"] = job["id"]
        return conn

    def replace_credential(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        connection_id: str,
        principal_id: str,
        credential: dict,
    ) -> dict:
        conn = self._get_for_owner(uow, workspace_id, connection_id)
        kind_e = ConnectionKind(conn["kind"])
        fmt = credential["format"]
        payload = credential["payload"]
        validate_manual_payload(kind_e, fmt, payload)
        if conn["state"] == ConnectionState.REVOKED.value:
            raise DomainError("invalid_state", "connection is revoked")

        next_ordinal = uow.rows.one(
            "SELECT COALESCE(MAX(ordinal), 0) + 1 AS n"
            " FROM credential_versions WHERE connection_id=%s",
            (connection_id,),
        )["n"]
        cred_id = new_id("credential_version")
        aad = credential_aad(
            workspace_id=workspace_id,
            connection_id=connection_id,
            credential_version_id=cred_id,
            fmt=fmt,
        )
        ct, key_id, nonce = self.vault.seal(json.dumps(payload).encode(), aad=aad)

        old_id = conn["current_credential_version_id"]
        if old_id:
            uow.conn.execute(
                "UPDATE credential_versions SET state='superseded' WHERE id=%s",
                (old_id,),
            )
        uow.credential_versions.insert(
            {
                "id": cred_id,
                "workspace_id": workspace_id,
                "connection_id": connection_id,
                "ordinal": next_ordinal,
                "state": "active",
                "format": fmt,
                "ciphertext": ct,
                "key_id": key_id,
                "nonce": nonce,
                "aad": aad,
                "fingerprint": _fingerprint(fmt, payload),
            }
        )
        # Atomic CAS: pointer + version + revocation epoch; observations and
        # outstanding grants on the old version die here.
        uow.conn.execute(
            "UPDATE connections SET current_credential_version_id=%s,"
            " version=version+1, revocation_epoch=revocation_epoch+1,"
            " health='unverified', capability_observations='{}',"
            " updated_at=now()"
            " WHERE id=%s AND version=%s",
            (cred_id, connection_id, conn["version"]),
        )
        uow.credential_grants.revoke_for_connection(workspace_id, connection_id)
        job = enqueue_job(
            uow,
            workspace_id=workspace_id,
            kind=JobKind.CONNECTION_VALIDATE,
            target_family=TargetFamily.CONNECTION,
            target_id=connection_id,
            dedupe_key=f"connection.validate:{connection_id}:{cred_id}",
            payload={
                "connection_id": connection_id,
                "credential_version_id": cred_id,
            },
        )
        uow.commit()
        out = uow.connections.get(workspace_id, connection_id)
        out["validation_job_id"] = job["id"]
        return out

    def disable(self, uow: SqlUnitOfWork, *, workspace_id: str, connection_id: str) -> dict:
        conn = self._get_for_owner(uow, workspace_id, connection_id)
        if conn["state"] != ConnectionState.CONFIGURED.value:
            raise DomainError("invalid_state", f"cannot disable from {conn['state']}")
        uow.connections.update(
            workspace_id, connection_id, {"state": ConnectionState.DISABLED.value}
        )
        uow.credential_grants.revoke_for_connection(workspace_id, connection_id)
        uow.commit()
        return uow.connections.get(workspace_id, connection_id)

    def enable(self, uow: SqlUnitOfWork, *, workspace_id: str, connection_id: str) -> dict:
        conn = self._get_for_owner(uow, workspace_id, connection_id)
        if conn["state"] != ConnectionState.DISABLED.value:
            raise DomainError("invalid_state", f"cannot enable from {conn['state']}")
        uow.connections.update(
            workspace_id, connection_id, {"state": ConnectionState.CONFIGURED.value}
        )
        uow.commit()
        return uow.connections.get(workspace_id, connection_id)

    def disconnect(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        connection_id: str,
        principal_id: str,
    ) -> dict:
        """Revoke + tombstone. Rejected while nonterminal executions depend
        on this connection's grants — keeps teardown authority over live
        compute (RFC MVP 18)."""
        conn = self._get_for_owner(uow, workspace_id, connection_id)
        if conn["state"] == ConnectionState.REVOKED.value:
            raise DomainError("invalid_state", "connection already revoked")

        live = uow.rows.one(
            "SELECT e.id FROM executions e"
            " JOIN credential_grants g ON g.execution_id = e.id"
            " WHERE g.connection_id=%s AND g.revoked_at IS NULL"
            " AND e.state IN ('preparing','started','stop_requested')"
            " LIMIT 1",
            (connection_id,),
        )
        if live is None:
            # Compute grants bind to the lease (the execution row does not
            # exist yet at allocate time) — a non-terminal lease with an
            # outstanding grant on this connection is live compute too.
            live = uow.rows.one(
                "SELECT g.id FROM credential_grants g"
                " JOIN executor_leases el ON el.id = g.lease_id"
                " WHERE g.connection_id=%s AND g.revoked_at IS NULL"
                " AND el.state IN ('allocating','ready','quiescing')"
                " LIMIT 1",
                (connection_id,),
            )
        if live is not None:
            raise DomainError(
                "invalid_state",
                "connection has live executions; stop them before disconnecting",
            )

        uow.conn.execute(
            "UPDATE credential_versions SET state='revoked', revoked_at=now()"
            " WHERE connection_id=%s AND state != 'revoked'",
            (connection_id,),
        )
        uow.credential_grants.revoke_for_connection(workspace_id, connection_id)
        uow.connections.update(
            workspace_id,
            connection_id,
            {
                "state": ConnectionState.REVOKED.value,
                "health": "unverified",
                "current_credential_version_id": None,
            },
        )
        uow.commit()
        return uow.connections.get(workspace_id, connection_id)

    def list_connections(
        self, uow: SqlUnitOfWork, *, workspace_id: str, include_revoked: bool = False
    ) -> list[dict]:
        rows = uow.connections.list(workspace_id)
        if not include_revoked:
            rows = [r for r in rows if r["state"] != ConnectionState.REVOKED.value]
        return [_safe_view(r) for r in rows]

    def get_connection(self, uow: SqlUnitOfWork, *, workspace_id: str, connection_id: str) -> dict:
        return _safe_view(self._get_for_owner(uow, workspace_id, connection_id))

    # -- grants / materialization ----------------------------------------

    def issue_grant(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        connection_id: str,
        purpose: str,
        principal_id: str | None = None,
        session_id: str | None = None,
        execution_id: str | None = None,
        lease_id: str | None = None,
        ttl_s: int = 3600,
    ) -> dict:
        conn = self._get_for_owner(uow, workspace_id, connection_id)
        conn_obj = _to_domain(conn)
        conn_obj.require_usable()
        if purpose not in (conn["allowed_purposes"] or []):
            raise DomainError(
                "validation_failed",
                f"purpose {purpose!r} not allowed for connection {connection_id}",
            )
        cred = uow.credential_versions.get(workspace_id, conn["current_credential_version_id"])
        if cred is None or cred["state"] != "active":
            raise DomainError("invalid_state", "no active credential version")
        grant = uow.credential_grants.insert(
            {
                "id": new_id("grant"),
                "workspace_id": workspace_id,
                "connection_id": connection_id,
                "credential_version_id": cred["id"],
                "purpose": purpose,
                "principal_id": principal_id,
                "session_id": session_id,
                "execution_id": execution_id,
                "lease_id": lease_id,
                "revocation_epoch": conn["revocation_epoch"],
                "expires_at": _after(ttl_s),
            }
        )
        return grant

    def redeem_grant(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        grant_id: str,
        lease_id: str | None = None,
        purpose: str | None = None,
        mark_redeemed: bool = True,
    ) -> MaterializedCredential:
        """Decrypt at the actual boundary. Rechecks current authority:
        grant live, epoch current, version active, connection usable."""
        grant = uow.rows.one(
            "SELECT * FROM credential_grants WHERE workspace_id=%s AND id=%s",
            (workspace_id, grant_id),
        )
        if grant is None:
            raise DomainError("not_found", "grant not found")
        if grant["revoked_at"] is not None or grant["expires_at"] <= _now():
            raise DomainError("unauthenticated", "grant expired or revoked")
        if mark_redeemed and grant["redeemed_at"] is not None:
            raise DomainError("idempotency_conflict", "grant already redeemed")
        if lease_id is not None and grant["lease_id"] not in (None, lease_id):
            raise DomainError("unauthenticated", "grant bound to a different lease")
        if purpose is not None and grant["purpose"] != purpose:
            raise DomainError("unauthenticated", "grant purpose mismatch")

        conn = uow.connections.get(workspace_id, grant["connection_id"])
        if conn is None or conn["state"] != ConnectionState.CONFIGURED.value:
            raise DomainError("connection_revoked", "connection not configured")
        if conn["revocation_epoch"] != grant["revocation_epoch"]:
            raise DomainError("unauthenticated", "grant issued under stale epoch")
        cred = uow.credential_versions.get(workspace_id, grant["credential_version_id"])
        if cred is None or cred["state"] != "active":
            raise DomainError("invalid_state", "credential version not active")

        plaintext = self.vault.open(
            bytes(cred["ciphertext"]),
            key_id=cred["key_id"],
            nonce=bytes(cred["nonce"]),
            aad=cred["aad"],
        )
        if mark_redeemed:
            uow.conn.execute(
                "UPDATE credential_grants SET redeemed_at=now() WHERE id=%s",
                (grant_id,),
            )
        return MaterializedCredential(
            connection_id=conn["id"],
            credential_version_id=cred["id"],
            format=cred["format"],
            payload=json.loads(plaintext),
            grant_id=grant_id,
        )

    def materialize(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        connection_id: str,
        purpose: str,
        session_id: str | None = None,
        execution_id: str | None = None,
        lease_id: str | None = None,
        principal_id: str | None = None,
    ) -> MaterializedCredential:
        """Issue + redeem in one transaction — worker-side helper."""
        grant = self.issue_grant(
            uow,
            workspace_id=workspace_id,
            connection_id=connection_id,
            purpose=purpose,
            principal_id=principal_id,
            session_id=session_id,
            execution_id=execution_id,
            lease_id=lease_id,
        )
        return self.redeem_grant(
            uow,
            workspace_id=workspace_id,
            grant_id=grant["id"],
            lease_id=lease_id,
            purpose=purpose,
        )

    def select_connection(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        kind: str,
        purpose: str,
    ) -> dict:
        """Explicit authorized candidates only — never ambient fallback."""
        candidates = [
            c
            for c in uow.connections.find_by_kind(workspace_id, kind)
            if c["state"] == ConnectionState.CONFIGURED.value
        ]
        for c in candidates:
            if purpose in (c["allowed_purposes"] or []) and (
                c["cooldown_until"] is None or c["cooldown_until"] <= _now()
            ):
                return c
        raise DomainError(
            "executor_unavailable",
            f"no configured {kind} connection with purpose {purpose!r}",
        )

    # -- observations -----------------------------------------------------

    def record_observation(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        connection_id: str,
        credential_version_id: str | None,
        kind: str,
        scope: str,
        result: dict,
        ttl_s: int | None = None,
    ) -> dict:
        return uow.connection_observations.insert(
            {
                "id": new_id("connection_observation"),
                "workspace_id": workspace_id,
                "connection_id": connection_id,
                "credential_version_id": credential_version_id,
                "kind": kind,
                "scope": scope,
                "result": result,
                "expires_at": _after(ttl_s) if ttl_s else None,
            }
        )

    # -- internals --------------------------------------------------------

    def _get_for_owner(self, uow, workspace_id: str, connection_id: str) -> dict:
        row = uow.connections.get(workspace_id, connection_id)
        if row is None:
            raise DomainError("not_found", "connection not found")
        return row


def _safe_view(conn: dict) -> dict:
    """Connection view: metadata only — ciphertext never joins the row."""
    out = dict(conn)
    return out


def _to_domain(row: dict):
    from control.domain.connections import Connection

    return Connection(
        id=row["id"],
        workspace_id=row["workspace_id"],
        kind=ConnectionKind(row["kind"]),
        label=row["label"],
        created_by=row["created_by"],
        allowed_principals=list(row["allowed_principals"] or []),
        allowed_purposes=list(row["allowed_purposes"] or []),
        acquisition=AcquisitionMethod(row["acquisition"]),
        state=ConnectionState(row["state"]),
        health=row["health"],
        current_credential_version_id=row["current_credential_version_id"],
        external_identity=dict(row["external_identity"] or {}),
        capability_observations=dict(row["capability_observations"] or {}),
        version=row["version"],
        revocation_epoch=row["revocation_epoch"],
    )


def _after(seconds: float):
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(seconds=seconds)


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)
