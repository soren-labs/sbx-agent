from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from protocol.capabilities import HarnessManifest


@dataclass(frozen=True)
class TurnContext:
    session_id: str
    turn_id: str
    execution_id: str
    lease_generation: int
    worktree_generation: int
    worktree: Path
    home: Path
    model: str
    prompt: str
    native_id: str | None = None
    deadline: float = 300
    settings: dict = field(default_factory=dict)


@dataclass(frozen=True)
class NativeInvocation:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    stdin: str | None = None


@dataclass(frozen=True)
class HarnessOutcome:
    verdict: str
    credential_health: str
    retry_advice: str
    native_id: str | None
    terminal_observed: bool


class Harness(Protocol):
    def describe(self) -> HarnessManifest: ...
    def prepare(self, context: TurnContext, credential_bundle: dict) -> dict[str, str]: ...
    def start_turn(self, context: TurnContext, env: dict) -> NativeInvocation: ...
    def resume_turn(self, context: TurnContext, env: dict) -> NativeInvocation: ...
    def normalize(self, frame: dict, state: dict) -> list[dict]: ...
    def classify_outcome(self, evidence: dict, state: dict) -> HarnessOutcome: ...
    def export_native_state(self, home: Path) -> list[Path]: ...
    def validate_native_state(self, home: Path, native_id: str) -> None: ...
    def release(self, context: TurnContext) -> None: ...
