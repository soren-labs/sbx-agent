"""Logical Worktree availability and Snapshot lifecycle (RFC 02)."""

from __future__ import annotations

from control.domain.lifecycle import Lifecycle, table

WORKTREE_AVAILABILITY = Lifecycle(
    "worktree",
    table(
        {
            "none": ("restoring", "unavailable"),
            "restoring": ("live", "unavailable", "none"),
            "live": ("checkpointed", "unavailable", "none"),
            "checkpointed": ("restoring", "unavailable"),
            "unavailable": ("restoring", "none"),
        }
    ),
    frozenset(),
)

SNAPSHOT = Lifecycle(
    "snapshot", table({"preparing": ("ready", "failed")}), frozenset({"ready", "failed"})
)
SNAPSHOT_KINDS = frozenset({"environment", "checkpoint"})
BARRIER_KINDS = frozenset({"capture", "checkpoint", "apply", "restore"})
