"""Connector result types shared by all Connection kinds."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from control.domain.errors import DomainError


@dataclass
class Observation:
    status: str  # ready | invalid | degraded | error
    external_identity: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    catalog: dict[str, Any] | None = None
    quota_consuming: bool = False
    retry_after: float | None = None


def require(credential: dict[str, Any], *fields: str, min_len: int = 8) -> None:
    for name in fields:
        value = credential.get(name)
        if not isinstance(value, str) or len(value.strip()) < min_len or len(value) > 8192:
            # The input value is never echoed back.
            raise DomainError(
                "validation_failed",
                f"credential field {name} is missing or malformed",
                details={"field": f"credential.{name}"},
            )
