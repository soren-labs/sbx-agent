"""Capability vocabulary (RFC 167 §03 Harness contract).

Capability entries answer ``supported | unsupported | unknown`` with evidence
metadata. Unknown is never enabled by default.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class CapabilityState(enum.StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


# Required capability names (RFC 167 §03). Every HarnessManifest carries an
# entry for each of these — absent real evidence they are ``unknown``.
CAPABILITY_NAMES: tuple[str, ...] = (
    "native_resume",
    "native_state_export",
    "account_portable_resume",
    "event_stream",
    "interrupt",
    "steer",
    "interactive_approval",
    "mcp",
    "skills",
    "attachments",
    "structured_output",
    "model_discovery",
    "effort_settings",
    "credential_writeback",
    "usage",
)


@dataclass(frozen=True)
class CapabilityEntry:
    name: str
    state: CapabilityState
    evidence_id: str | None = None  # e.g. conformance-test or observation ref
    observed_at: str | None = None  # ISO8601
    cli_version: str | None = None
    mode: str | None = None  # e.g. structured_output: native|prompt_only
    limitation: str | None = None

    def to_dict(self) -> dict:
        out = {"name": self.name, "state": self.state.value}
        for key in ("evidence_id", "observed_at", "cli_version", "mode", "limitation"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    @staticmethod
    def supported(name: str, **kw) -> CapabilityEntry:
        return CapabilityEntry(name, CapabilityState.SUPPORTED, **kw)

    @staticmethod
    def unsupported(name: str, **kw) -> CapabilityEntry:
        return CapabilityEntry(name, CapabilityState.UNSUPPORTED, **kw)

    @staticmethod
    def unknown(name: str, **kw) -> CapabilityEntry:
        return CapabilityEntry(name, CapabilityState.UNKNOWN, **kw)


@dataclass(frozen=True)
class HarnessManifest:
    """What the installed Harness + CLI can verifiably do."""

    provider_id: str
    adapter_version: str
    cli_version: str | None = None
    cli_digest: str | None = None
    transport: str = "jsonl"  # jsonl|acp|official_server|text
    support_tier: str = "verified"  # verified|provisional|disabled
    capabilities: tuple[CapabilityEntry, ...] = ()
    native_state_versions: tuple[int, ...] = (1,)

    def capability(self, name: str) -> CapabilityEntry:
        for entry in self.capabilities:
            if entry.name == name:
                return entry
        return CapabilityEntry(name, CapabilityState.UNKNOWN)

    def is_supported(self, name: str) -> bool:
        return self.capability(name).state is CapabilityState.SUPPORTED

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "adapter_version": self.adapter_version,
            "cli_version": self.cli_version,
            "cli_digest": self.cli_digest,
            "transport": self.transport,
            "support_tier": self.support_tier,
            "capabilities": [c.to_dict() for c in self.capabilities],
            "native_state_versions": list(self.native_state_versions),
        }

    @staticmethod
    def from_dict(raw: dict) -> HarnessManifest:
        caps = tuple(
            CapabilityEntry(
                name=str(c.get("name", "")),
                state=CapabilityState(c.get("state", "unknown")),
                evidence_id=c.get("evidence_id"),
                observed_at=c.get("observed_at"),
                cli_version=c.get("cli_version"),
                mode=c.get("mode"),
                limitation=c.get("limitation"),
            )
            for c in raw.get("capabilities") or ()
        )
        return HarnessManifest(
            provider_id=str(raw.get("provider_id", "")),
            adapter_version=str(raw.get("adapter_version", "")),
            cli_version=raw.get("cli_version"),
            cli_digest=raw.get("cli_digest"),
            transport=str(raw.get("transport", "jsonl")),
            support_tier=str(raw.get("support_tier", "verified")),
            capabilities=caps,
            native_state_versions=tuple(int(v) for v in raw.get("native_state_versions") or (1,)),
        )


@dataclass(frozen=True)
class CapabilityObservation:
    """Result of ``harness.discover(context)`` — catalog metadata only, never
    model reasoning."""

    provider_id: str
    models: tuple[str, ...] = ()
    source: str = "cli"  # cli|connector|static
    observed_at: str | None = None
    scope: str = "account"
    free_models: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "models": list(self.models),
            "free_models": list(self.free_models),
            "source": self.source,
            "observed_at": self.observed_at,
            "scope": self.scope,
            "extra": self.extra,
        }
