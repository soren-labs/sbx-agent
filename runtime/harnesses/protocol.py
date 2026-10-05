"""Provider-neutral Harness interface. The supervisor executes returned invocations.

No adapter performs model requests or reasoning; it only prepares the official
CLI, builds its invocation, interprets its frames and classifies its outcome.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class Capability:
    status: str  # supported | unsupported | unknown
    evidence: str = ""
    limitation: str = ""
    mode: str = ""


@dataclass
class HarnessManifest:
    provider_id: str
    adapter_version: str
    cli_version: str
    distribution: str
    transport: str
    support_tier: str
    capabilities: dict[str, Capability]
    native_state_versions: list[str] = field(default_factory=list)
    credential_methods: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["capabilities"] = {k: asdict(v) for k, v in self.capabilities.items()}
        return data


@dataclass
class TurnContext:
    session_id: str
    turn_id: str
    execution_id: str
    operation_id: str
    lease_generation: int
    worktree: Path
    home: Path
    prompt: str
    model: str | None = None
    effort: str | None = None
    native_binding: dict[str, Any] | None = None
    deadline_seconds: float = 3600.0
    result_contract: dict[str, Any] | None = None


@dataclass
class PreparedHarness:
    home: Path
    env: dict[str, str]
    secrets: list[str] = field(default_factory=list, repr=False)
    credential_files: list[Path] = field(default_factory=list)


@dataclass
class NativeInvocation:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    stdin: bytes | None = None


@dataclass
class HarnessOutcome:
    verdict: str  # success | failure | unknown
    credential_health: str = "ok"  # ok | invalid | rate_limited | unknown
    error_code: str | None = None
    message: str = ""
    retry_advice: str = "none"


class HarnessError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Harness(Protocol):
    provider_id: str

    def describe(self) -> HarnessManifest: ...

    def prepare(self, context: TurnContext, credentials: dict[str, Any]) -> PreparedHarness: ...

    def start_turn(self, context: TurnContext, prepared: PreparedHarness) -> NativeInvocation: ...

    def resume_turn(
        self, context: TurnContext, prepared: PreparedHarness, binding: dict[str, Any]
    ) -> NativeInvocation: ...

    def new_state(self, context: TurnContext) -> dict[str, Any]: ...

    def normalize(self, line: str, state: dict[str, Any]) -> list[dict[str, Any]]: ...

    def finish(self, state: dict[str, Any]) -> list[dict[str, Any]]: ...

    def classify_outcome(
        self, evidence: dict[str, Any], state: dict[str, Any]
    ) -> HarnessOutcome: ...

    def discover(self, prepared: PreparedHarness) -> NativeInvocation | None: ...

    def native_state_paths(self, home: Path) -> list[Path]: ...

    def release(self, prepared: PreparedHarness) -> list[str]: ...


def obs(type: str, **payload: Any) -> dict[str, Any]:
    return {"type": type, "payload": payload}


AUTH_NEEDLES = (
    "invalid api key",
    "incorrect api key",
    "unauthorized",
    "unauthenticated",
    "401",
    "authentication",
    "not logged in",
)
RATE_NEEDLES = ("429", "rate limit", "rate_limit", "too many requests", "quota", "overloaded")


def classify_text(text: str) -> str:
    lower = text.lower()
    if any(n in lower for n in AUTH_NEEDLES):
        return "invalid"
    if any(n in lower for n in RATE_NEEDLES):
        return "rate_limited"
    return "unknown"


def bounded(value: Any, limit: int = 4000) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…[truncated]"
    if isinstance(value, dict):
        return {k: bounded(v, limit) for k, v in list(value.items())[:50]}
    if isinstance(value, list):
        return [bounded(v, limit) for v in value[:50]]
    return value
