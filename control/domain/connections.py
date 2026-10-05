"""One Connection domain and encrypted CredentialVersions (RFC 167 §06).

All external authority — compute, source control, inference — is a Connection.
Manual acquisition is the baseline: Modal token pair, GitHub token, OpenCode
Zen key, optional Codex native bundle. No ambient fallback, no Integration
aggregate.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .errors import DomainError


class ConnectionKind(enum.StrEnum):
    MODAL = "modal"
    GITHUB = "github"
    OPENCODE_ZEN = "opencode_zen"
    CODEX = "codex"


class AcquisitionMethod(enum.StrEnum):
    MANUAL = "manual"
    NATIVE_UPLOAD = "native_upload"
    OAUTH = "oauth"
    DEVICE = "device"


class ConnectionState(enum.StrEnum):
    CONFIGURED = "configured"
    DISABLED = "disabled"
    REVOKED = "revoked"


class ConnectionHealth(enum.StrEnum):
    UNVERIFIED = "unverified"
    VERIFYING = "verifying"
    READY = "ready"
    DEGRADED = "degraded"
    REAUTH_REQUIRED = "reauth_required"


# Exact plaintext destinations per kind; everything else is forbidden.
CREDENTIAL_DESTINATIONS: dict[ConnectionKind, str] = {
    ConnectionKind.MODAL: "executor_worker",
    ConnectionKind.GITHUB: "delivery_worker",
    ConnectionKind.OPENCODE_ZEN: "runtime_execution",
    ConnectionKind.CODEX: "runtime_execution",
}

# Credential schema each manual kind accepts (validated before sealing).
CREDENTIAL_FORMATS: dict[ConnectionKind, dict[str, frozenset[str]]] = {
    ConnectionKind.MODAL: {"token_pair": frozenset({"token_id", "token_secret"})},
    ConnectionKind.GITHUB: {"personal_token": frozenset({"token"})},
    ConnectionKind.OPENCODE_ZEN: {"api_key": frozenset({"api_key"})},
    ConnectionKind.CODEX: {"auth_bundle": frozenset({"files"})},
}


def validate_manual_payload(kind: ConnectionKind, fmt: str, payload: dict) -> None:
    formats = CREDENTIAL_FORMATS.get(kind)
    if formats is None or fmt not in formats:
        raise DomainError(
            "validation_failed",
            f"unsupported credential format {fmt!r} for {kind.value}",
        )
    missing = formats[fmt] - set(payload)
    if missing:
        raise DomainError(
            "validation_failed",
            f"credential payload missing fields: {sorted(missing)}",
        )
    for key, value in payload.items():
        if not isinstance(value, str) or not value.strip():
            raise DomainError("validation_failed", f"credential field {key!r} must be non-empty")


@dataclass
class Connection:
    id: str
    workspace_id: str
    kind: ConnectionKind
    label: str | None
    created_by: str
    allowed_principals: list[str]
    allowed_purposes: list[str]
    acquisition: AcquisitionMethod
    state: ConnectionState
    health: ConnectionHealth
    current_credential_version_id: str | None = None
    external_identity: dict = field(default_factory=dict)
    capability_observations: dict = field(default_factory=dict)
    version: int = 1
    revocation_epoch: int = 0
    cooldown_until: object = None
    created_at: object = None
    updated_at: object = None

    def require_usable(self) -> None:
        if self.state is ConnectionState.REVOKED:
            raise DomainError("connection_revoked", f"connection {self.id} is revoked")
        if self.state is ConnectionState.DISABLED:
            raise DomainError("invalid_state", f"connection {self.id} is disabled")


@dataclass
class CredentialVersion:
    id: str
    workspace_id: str
    connection_id: str
    ordinal: int
    state: str  # active|superseded|revoked
    format: str
    ciphertext: bytes
    key_id: str
    nonce: bytes
    aad: dict
    fingerprint: str  # safe metadata (e.g. token_id for modal), never secret
    expires_at: object = None
    revoked_at: object = None
    created_at: object = None


@dataclass
class CredentialGrant:
    """Purpose-bound attenuated grant; redeemed at the actual boundary."""

    id: str
    workspace_id: str
    connection_id: str
    credential_version_id: str
    purpose: str
    principal_id: str | None
    session_id: str | None
    execution_id: str | None
    lease_id: str | None
    revocation_epoch: int
    expires_at: object
    redeemed_at: object = None
    revoked_at: object = None
