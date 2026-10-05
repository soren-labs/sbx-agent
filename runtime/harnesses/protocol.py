"""Provider-neutral Harness interface (RFC 167 §03 contract table).

The daemon supervisor executes returned ``NativeInvocation``s; Harness
adapters own official-CLI credentials/config, invocation argv, native
context and event interpretation. They never allocate executors, define
Session identity, or reason through a direct model API.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from protocol.capabilities import CapabilityObservation, HarnessManifest
from protocol.events import Observation
from protocol.manifests import NativeContextBinding, NativeStateManifest

ADAPTER_VERSION = "1.0.0"


@dataclass(frozen=True)
class CredentialBundle:
    """Materialized credential files + env for one provider.

    ``files`` maps $HOME-relative paths to content (never logged);
    ``env`` is merged into the invocation's allowlisted environment.
    """

    files: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    lease_ref: str | None = None  # control-plane credential lease identity


@dataclass(frozen=True)
class TurnContext:
    """Everything a Harness needs to construct an official invocation."""

    session_id: str
    turn_id: str
    execution_id: str
    attempt_ordinal: int
    effect_id: str
    lease_generation: int
    worktree_root: Path
    worktree_generation: int
    prompt: str
    model: str | None = None
    effort: str | None = None
    instructions: str | None = None
    instructions_digest: str | None = None
    attachments: tuple[dict, ...] = ()
    result_contract: dict | None = None
    deadline: float | None = None
    native_binding: NativeContextBinding | None = None
    tool_grants: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedHarness:
    """Isolated, materialized execution environment for one provider."""

    home: Path  # isolated HOME; never the host HOME
    xdg_data_home: Path
    xdg_config_home: Path
    env: dict[str, str]  # allowlisted env for every invocation
    provider_id: str
    files_written: tuple[str, ...] = ()


@dataclass(frozen=True)
class NativeInvocation:
    """An official-CLI invocation the supervisor may execute."""

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    stdin: str = "devnull"  # devnull|pipe
    transport: str = "jsonl"  # stdout frame format
    kind: str = "turn"  # turn|discover|cancel


class OutcomeKind(enum.StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    INTERRUPTED = "interrupted"  # cancelled/stopped by request
    UNKNOWN = "unknown"  # ambiguous; automatic continuation forbidden


@dataclass(frozen=True)
class HarnessOutcome:
    """``classify_outcome`` result — evidence, not a business verdict."""

    outcome: OutcomeKind
    reason: str | None = None  # domain TURN_REASONS vocabulary
    credential_health: str = "unchanged"  # unchanged|invalid|rate_limited
    retry_advice: str = "none"  # none|retry|retry_later|reallocate|manual
    native_binding: NativeContextBinding | None = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {
            "outcome": self.outcome.value,
            "credential_health": self.credential_health,
            "retry_advice": self.retry_advice,
        }
        if self.reason:
            out["reason"] = self.reason
        if self.native_binding is not None:
            out["native_binding"] = self.native_binding.to_dict()
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True)
class ProcessEvidence:
    """Supervisor-side terminal facts fed to ``classify_outcome``."""

    exit_code: int | None
    signal: int | None
    duration_ms: int
    stderr_tail: str = ""
    cancel_requested: bool = False
    timed_out: bool = False
    observations: tuple[Observation, ...] = ()


class UnsupportedCapability(Exception):
    def __init__(self, capability: str, provider_id: str) -> None:
        super().__init__(f"{provider_id}: capability {capability} unsupported")
        self.capability = capability
        self.provider_id = provider_id


class ContextMismatch(Exception):
    """Requested native resume target missing/incompatible — never fork a new
    conversation silently."""


class Harness(Protocol):
    """The provider-neutral adapter contract."""

    provider_id: str

    def describe(self) -> HarnessManifest: ...

    def prepare(self, context: TurnContext, credentials: CredentialBundle) -> PreparedHarness: ...

    def start_turn(self, context: TurnContext, prepared: PreparedHarness) -> NativeInvocation: ...

    def resume_turn(
        self,
        context: TurnContext,
        prepared: PreparedHarness,
        binding: NativeContextBinding,
    ) -> NativeInvocation: ...

    def normalize(self, frame: str, state: NormalizeState) -> list[Observation]: ...

    def classify_outcome(self, evidence: ProcessEvidence) -> HarnessOutcome: ...

    def discover(self, prepared: PreparedHarness) -> CapabilityObservation: ...

    def interrupt(self, execution_id: str) -> None: ...

    def steer(self, context: TurnContext, text: str) -> None: ...

    def respond_approval(self, request_id: str, decision: str) -> None: ...

    def export_native_state(self, binding: NativeContextBinding) -> NativeStateManifest | None: ...

    def validate_native_state(self, manifest: NativeStateManifest) -> bool: ...

    def export_refreshed_credentials(self, base_version: str) -> CredentialBundle | None: ...

    def release(self, prepared: PreparedHarness) -> list[str]: ...


@dataclass
class NormalizeState:
    """Mutable per-invocation normalize scratch owned by the supervisor."""

    observations: list[Observation] = field(default_factory=list)
    item_seq: int = 0
    data: dict = field(default_factory=dict)

    def next_item_id(self, prefix: str = "item") -> str:
        self.item_seq += 1
        return f"{prefix}_{self.item_seq}"
