"""SOR-179 canonical ``reasoning_effort``: levels and provider capability.

One canonical request field — ``reasoning_effort`` on agent create — is
mapped onto each provider CLI's native effort surface at turn time:

- ``codex``: ``model_reasoning_effort`` in the rendered ``config.toml``
  (durable in ``$CODEX_HOME`` — first turns and ``codex exec resume``
  inherit it identically).
- ``antigravity``: ``agy --effort <level>`` on every turn's argv.
- ``grok``: ``grok --effort <level>`` on every turn's argv.
- ``opencode`` / ``devin``: no verified effort surface at the pinned CLI
  versions (``runtime/packages.txt`` / SOR-175 lock). A declared effort is
  refused — ``unsupported`` at ``POST /v1/agents``, a failed ``runner init``
  as the in-sandbox backstop — never silently ignored.
"""

from __future__ import annotations

from typing import Any

CANONICAL_EFFORTS: tuple[str, ...] = ("low", "medium", "high")

# Providers that can honor a declared effort natively for every canonical
# level. Providers absent from the set (or missing a level) refuse the
# request explicitly instead of running with a silently dropped effort.
SUPPORTED_EFFORTS: dict[str, frozenset[str]] = {
    "codex": frozenset(CANONICAL_EFFORTS),
    "antigravity": frozenset(CANONICAL_EFFORTS),
    "grok": frozenset(CANONICAL_EFFORTS),
    "opencode": frozenset(),
    "devin": frozenset(),
}


def supported_efforts(provider: str) -> tuple[str, ...]:
    """Canonical effort levels ``provider`` honors, in canonical order."""
    supported = SUPPORTED_EFFORTS.get(provider, frozenset())
    return tuple(level for level in CANONICAL_EFFORTS if level in supported)


def normalize_effort(value: Any) -> str | None:
    """Return the canonical level for ``value``; ``None`` when unset.

    Anything non-canonical raises ``ValueError`` — an unrecognized level is
    a request error, never coerced or dropped.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text not in CANONICAL_EFFORTS:
        raise ValueError(
            f"unknown reasoning_effort {text!r}; canonical levels: {list(CANONICAL_EFFORTS)}"
        )
    return text


def effort_error(provider: str, effort: str | None) -> str | None:
    """Why ``effort`` cannot be honored on ``provider``; ``None`` when it can."""
    if effort is None:
        return None
    if effort not in SUPPORTED_EFFORTS.get(provider, frozenset()):
        levels = supported_efforts(provider)
        if levels:
            return (
                f"provider {provider!r} does not support reasoning_effort {effort!r} "
                f"(supported: {list(levels)})"
            )
        return f"provider {provider!r} does not support reasoning_effort"
    return None
