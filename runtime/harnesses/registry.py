"""Harness registry — provider_id → adapter factory (RFC 167 §03).

Only providers with verified gates are registered; unknown providers refuse
to enroll rather than pretending support.
"""

from __future__ import annotations

from pathlib import Path

from .opencode import PROVIDER_ID as OPENCODE_PROVIDER
from .opencode import OpencodeHarness
from .protocol import Harness


def registered_providers() -> tuple[str, ...]:
    return (OPENCODE_PROVIDER,)


def get_harness(provider_id: str, state_root: Path) -> Harness:
    if provider_id == OPENCODE_PROVIDER:
        return OpencodeHarness(state_root)
    raise KeyError(f"no registered harness for provider {provider_id!r}")


def manifests(state_root: Path) -> list[dict]:
    """Harness manifests reported in the runtime ``hello``."""
    out = []
    for provider_id in registered_providers():
        out.append(get_harness(provider_id, state_root).describe().to_dict())
    return out
