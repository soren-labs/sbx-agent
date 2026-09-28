"""User-facing provider readiness (SOR-258).

Collapses the internal provider signals — deployment runtime evidence
(``runtime.enabled`` / ``runtime.status``) and credential connection
state (``connection.status`` + per-account scheduler statuses) — into
the single word a user should act on:

    ready        a session can start right now (enabled, runtime sane,
                 at least one scheduler-eligible account)
    needs_login  a credential is missing or credential-broken — fix it
                 with ``sbx auth login`` / ``relink`` / ``import``
    busy         credentials are fine but capacity is exhausted
                 (accounts active-and-full or cooling)
    disabled     the deployment does not run this provider
    unhealthy    deployment evidence is degraded (image/build/host CLI
                 lane) — a deploy-side problem login cannot fix

``/v1/providers`` rows intentionally keep the internal fields distinct;
this normalization is a client/operator surface, not an API contract
change — callers compute it from the same row the API already returns.

Precedence: disabled > unhealthy > ready > busy/needs_login — a
deployment-side problem is always the most urgent signal, live
schedulable capacity overrides any remaining account breakage, and the
login-vs-busy split distinguishes "credentials broken" from "capacity
full". A ``degraded`` connection with unavailable account detail lands
on ``busy`` by convention (the accounts demonstrably exist; verified-
capacity-exhausted is the actionable read).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

READY = "ready"
NEEDS_LOGIN = "needs_login"
BUSY = "busy"
DISABLED = "disabled"
UNHEALTHY = "unhealthy"

READINESS_STATES: tuple[str, ...] = (READY, NEEDS_LOGIN, BUSY, DISABLED, UNHEALTHY)

# Scheduler-eligible account statuses that signal *capacity* rather than
# *credential* breakage: the account passed verification; it is either
# running at its slot cap or cooling between failures.
_CAPACITY_STATUSES = frozenset({"active", "cooling"})


def provider_readiness(
    row: Mapping[str, object],
    *,
    account_statuses: Sequence[str] | None = None,
) -> str:
    """Reduce one ``/v1/providers`` row to a readiness word.

    ``account_statuses`` is the optional per-account ``status`` list for
    that provider (e.g. from ``/v1/accounts``); when None — the caller's
    key lacked admin scope or the deployment predates the accounts
    surface — a ``degraded`` connection resolves to ``busy`` rather than
    guessing at credential breakage.
    """
    runtime = row.get("runtime")
    connection = row.get("connection")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    connection = connection if isinstance(connection, Mapping) else {}

    if not runtime.get("enabled", False) or runtime.get("status") == "disabled":
        return DISABLED
    if runtime.get("status") == "degraded":
        return UNHEALTHY

    conn = connection.get("status")
    if conn == "connected":
        return READY
    if conn == "degraded":
        if account_statuses is None:
            return BUSY
        if any(status in _CAPACITY_STATUSES for status in account_statuses):
            return BUSY
        return NEEDS_LOGIN
    if conn == "not_connected":
        return NEEDS_LOGIN
    # Unknown/legacy connection payload — never claim ready on garbage.
    return UNHEALTHY


def provider_readiness_map(
    rows: Sequence[Mapping[str, object]],
    *,
    account_statuses: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, str]:
    """Readiness word per provider row, keyed by provider name."""
    out: dict[str, str] = {}
    for row in rows:
        provider = row.get("provider")
        if not isinstance(provider, str):
            continue
        statuses = account_statuses.get(provider) if account_statuses else None
        out[provider] = provider_readiness(row, account_statuses=statuses)
    return out
