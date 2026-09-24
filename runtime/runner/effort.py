"""SOR-179/SOR-204 canonical ``reasoning_effort``: levels and provider capability.

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

SOR-204 widens the canonical ladder (``none``..``max``) and adds the
explicit canonical→native token map each adapter applies at turn time.
``SUPPORTED_EFFORTS`` is the *static verified floor* — the authoritative
per-account capability is the discovery layer in ``control/capabilities``;
this table applies only when discovery has no live data for the account.
"""

from __future__ import annotations

from typing import Any

CANONICAL_EFFORTS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

# Providers that can honor a declared effort natively for every canonical
# level. Providers absent from the set (or missing a level) refuse the
# request explicitly instead of running with a silently dropped effort.
SUPPORTED_EFFORTS: dict[str, frozenset[str]] = {
    "codex": frozenset({"low", "medium", "high"}),
    "antigravity": frozenset({"low", "medium", "high"}),
    "grok": frozenset({"low", "medium", "high"}),
    "opencode": frozenset(),
    "devin": frozenset(),
}

# Explicit canonical -> provider-native effort token (SOR-204). Identity by
# default — the pinned CLIs spell the canonical levels identically — but a
# provider whose CLI uses a different token overrides just that entry here,
# and the adapters translate before emitting argv/config.
NATIVE_EFFORT_TOKENS: dict[str, dict[str, str]] = {
    "codex": {},
    "antigravity": {},
    "grok": {},
}

# Provider-native spellings -> canonical level, for the capability parser.
# Anything absent here is not a recognized effort token.
_NATIVE_TO_CANONICAL: dict[str, str] = {
    "off": "none",
    "none": "none",
    "disabled": "none",
    "disable": "none",
    "min": "minimal",
    "minimal": "minimal",
    "low": "low",
    "med": "medium",
    "medium": "medium",
    "moderate": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "x-high": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
    "ultra": "xhigh",
    "very-high": "xhigh",
    "max": "max",
    "maximal": "max",
    "maximum": "max",
}


def native_effort(provider: str, effort: str) -> str:
    """Provider-native argv/config token for canonical ``effort``.

    Identity by default; ``NATIVE_EFFORT_TOKENS`` overrides per provider.
    """
    return NATIVE_EFFORT_TOKENS.get(provider, {}).get(effort, effort)


def canonical_effort(provider: str, token: str) -> str | None:
    """Canonical level for a provider-native ``token``; ``None`` when unknown.

    Checks the provider's explicit native map first (so ``ultra`` can mean
    ``xhigh`` on one provider while remaining unrecognized on another),
    then the shared spelling table.
    """
    text = str(token).strip().lower()
    if not text:
        return None
    for canonical, native in NATIVE_EFFORT_TOKENS.get(provider, {}).items():
        if native.lower() == text:
            return canonical
    return _NATIVE_TO_CANONICAL.get(text)


def is_effort_token(provider: str, token: str) -> bool:
    return canonical_effort(provider, token) is not None


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


def split_effort_suffix(model: str) -> tuple[str, str | None]:
    """Split a trailing effort token off a model id, e.g. ``-low``.

    ``gemini-3.8-flash-low`` -> ``("gemini-3.8-flash", "low")``. The stem is
    returned as-is when no canonical effort suffix is present.
    """
    text = model.strip()
    if "-" not in text:
        return text, None
    stem, _, tail = text.rpartition("-")
    canonical = _NATIVE_TO_CANONICAL.get(tail.lower())
    if canonical is None or not stem:
        return text, None
    return stem, canonical
