"""Connector boundary: provider probes for Connection validation and
capability observation (RFC 167 §06 — connectors are effect boundaries,
not a parallel Integration domain)."""

from __future__ import annotations

from control.connectors.base import (
    ConnectorRegistry,
    ConnectorResult,
    CredentialConnector,
)

__all__ = [
    "ConnectorRegistry",
    "ConnectorResult",
    "CredentialConnector",
    "get_connector_registry",
    "register_builtin_connectors",
]

_REGISTRY = ConnectorRegistry()


def get_connector_registry() -> ConnectorRegistry:
    return _REGISTRY


def register_builtin_connectors(registry: ConnectorRegistry | None = None) -> ConnectorRegistry:
    """Register the manual-MVP connectors. Import-time side effect free —
    callers opt in explicitly."""
    reg = registry or _REGISTRY
    if not reg.has("modal"):
        from control.connectors.modal import ModalConnector

        reg.register(ModalConnector())
    if not reg.has("github"):
        from control.connectors.github import GithubConnector

        reg.register(GithubConnector())
    if not reg.has("opencode_zen"):
        from control.connectors.opencode_zen import OpencodeZenConnector

        reg.register(OpencodeZenConnector())
    return reg
