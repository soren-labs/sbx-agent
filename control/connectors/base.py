"""Connector contract: minimal documented probes, safe errors.

Validation is a Job-scoped side effect — never run inside a GET — and
returns only safe identity/capability metadata. Plaintext credential
payloads arrive as the decrypted ``payload`` dict and must never be
returned, logged, or re-serialized.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from control.domain.errors import DomainError


@dataclass
class ConnectorResult:
    """Safe validation/capability observation."""

    ok: bool
    external_identity: dict = field(
        default_factory=dict
    )  # e.g. {"login": "...", "workspace": "..."}
    capabilities: dict = field(default_factory=dict)  # resource/purpose scoped
    reason: str | None = None  # stable machine reason on failure
    message: str | None = None  # safe human text — never contains material


class CredentialConnector(Protocol):
    """One per ConnectionKind. ``kind`` matches connections.kind."""

    kind: str

    def validate(self, fmt: str, payload: dict) -> ConnectorResult:
        """Minimal documented probe of the submitted credential."""
        ...

    def discover_models(self, payload: dict) -> list[dict] | None:
        """Optional model catalog probe (inference connectors). Returns
        [{id, free, usable}] or None when unsupported."""
        return None


class ConnectorRegistry:
    def __init__(self):
        self._connectors: dict[str, CredentialConnector] = {}

    def register(self, connector: CredentialConnector) -> None:
        self._connectors[connector.kind] = connector

    def has(self, kind: str) -> bool:
        return kind in self._connectors

    def get(self, kind: str) -> CredentialConnector:
        conn = self._connectors.get(kind)
        if conn is None:
            raise DomainError("validation_failed", f"no connector for kind {kind!r}")
        return conn

    def kinds(self) -> list[str]:
        return sorted(self._connectors)
