"""Execution attempt and ExecutorLease lifecycles (RFC 02, 03)."""

from __future__ import annotations

from control.domain.lifecycle import Lifecycle, table

EXECUTION = Lifecycle(
    "execution",
    table(
        {
            "preparing": ("started", "failed", "cancelled", "unknown"),
            "started": ("stop_requested", "succeeded", "failed", "unknown"),
            "stop_requested": ("cancelled", "unknown", "succeeded", "failed"),
        }
    ),
    frozenset({"succeeded", "failed", "cancelled", "unknown"}),
)

LEASE = Lifecycle(
    "executor_lease",
    table(
        {
            "allocating": ("ready", "quiescing", "lost"),
            "ready": ("quiescing", "lost"),
            "quiescing": ("ready", "released", "lost"),
        }
    ),
    frozenset({"released", "lost"}),
)

LIVE_LEASE_STATES = frozenset({"allocating", "ready", "quiescing"})
LIVE_EXECUTION_STATES = frozenset({"preparing", "started", "stop_requested"})
