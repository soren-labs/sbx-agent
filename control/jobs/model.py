"""Job claim records and handler outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Claim:
    job_id: str
    kind: str
    generation: int
    holder: str
    expires_at: datetime
    workspace_id: str
    effect_id: str
    attempts: int
    reclaimed: bool
    job: dict[str, Any] = field(repr=False)

    @property
    def input(self) -> dict[str, Any]:
        return self.job.get("input") or {}

    def target(self, column: str) -> str:
        return self.job[column]


@dataclass(frozen=True)
class Succeeded:
    result: dict[str, Any] | None = None


@dataclass(frozen=True)
class Retry:
    code: str
    message: str = ""
    delay: float | None = None
    retry_after: float | None = None


@dataclass(frozen=True)
class Continue:
    """Release the claim into a due continuation (polling/waiting), no failure."""

    delay: float = 1.0
    note: str = ""
    input: dict[str, Any] | None = None


@dataclass(frozen=True)
class Failed:
    code: str
    message: str = ""


@dataclass(frozen=True)
class Cancelled:
    reason: str = ""


Outcome = Succeeded | Retry | Continue | Failed | Cancelled
