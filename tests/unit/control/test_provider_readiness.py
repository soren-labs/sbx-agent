"""SOR-258: normalized provider readiness.

Collapses the internal ``/v1/providers`` signals (runtime.enabled/
runtime.status, connection.status, per-account scheduler statuses) into
the user-facing Ready / Needs login / Busy / Disabled / Unhealthy
vocabulary — the internal fields stay distinct on the wire; only the
operator read is normalized.
"""

from __future__ import annotations

from typing import Any

from control.provider_readiness import (
    BUSY,
    DISABLED,
    NEEDS_LOGIN,
    READINESS_STATES,
    READY,
    UNHEALTHY,
    provider_readiness,
    provider_readiness_map,
)


def _row(
    *,
    enabled: bool = True,
    runtime: str = "ready",
    connection: str = "connected",
    total: int = 0,
    available: int = 0,
) -> dict[str, Any]:
    return {
        "provider": "agy",
        "runtime": {"enabled": enabled, "status": runtime},
        "connection": {
            "status": connection,
            "accounts_total": total,
            "accounts_available": available,
        },
    }


def test_states_vocabulary() -> None:
    assert READINESS_STATES == (READY, NEEDS_LOGIN, BUSY, DISABLED, UNHEALTHY)


def test_disabled_when_not_enabled() -> None:
    row = _row(enabled=False, runtime="disabled", connection="connected", available=1)
    assert provider_readiness(row) == DISABLED


def test_runtime_degraded_is_unhealthy() -> None:
    # Deploy-side evidence problems beat connection state entirely.
    row = _row(runtime="degraded", connection="connected", available=1)
    assert provider_readiness(row) == UNHEALTHY


def test_connected_is_ready() -> None:
    row = _row(connection="connected", total=2, available=1)
    assert provider_readiness(row) == READY


def test_unknown_runtime_with_live_account_is_ready() -> None:
    # Live schedulable evidence outweighs missing deploy records.
    row = _row(runtime="unknown", connection="connected", available=1)
    assert provider_readiness(row) == READY


def test_not_connected_needs_login() -> None:
    row = _row(connection="not_connected", total=0, available=0)
    assert provider_readiness(row) == NEEDS_LOGIN


def test_degraded_with_only_broken_accounts_needs_login() -> None:
    row = _row(connection="degraded", total=2, available=0)
    assert provider_readiness(row, account_statuses=["invalid", "unverified"]) == NEEDS_LOGIN


def test_degraded_with_active_account_is_busy() -> None:
    row = _row(connection="degraded", total=1, available=0)
    assert provider_readiness(row, account_statuses=["active"]) == BUSY


def test_degraded_with_cooling_account_is_busy() -> None:
    row = _row(connection="degraded", total=1, available=0)
    assert provider_readiness(row, account_statuses=["cooling"]) == BUSY


def test_degraded_without_account_detail_defaults_busy() -> None:
    # Convention: accounts demonstrably exist — capacity, not credentials,
    # is the actionable read when detail is unavailable.
    row = _row(connection="degraded", total=1, available=0)
    assert provider_readiness(row, account_statuses=None) == BUSY


def test_mixed_accounts_busy_beats_needs_login() -> None:
    row = _row(connection="degraded", total=2, available=0)
    assert provider_readiness(row, account_statuses=["invalid", "cooling"]) == BUSY


def test_malformed_row_never_claims_ready() -> None:
    assert provider_readiness({}) == DISABLED  # no enabled-runtime evidence
    assert (
        provider_readiness({"connection": {"status": "bogus"}, "runtime": {"enabled": True}})
        == UNHEALTHY
    )


def test_readiness_map_keyed_by_provider() -> None:
    rows = [
        {**_row(connection="connected"), "provider": "codex"},
        {**_row(enabled=False, runtime="disabled", connection="not_connected"), "provider": "agy"},
    ]
    out = provider_readiness_map(rows)
    assert out == {"codex": READY, "agy": DISABLED}
