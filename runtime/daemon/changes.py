"""Worktree change capture/apply (implemented in the ChangeSet phase)."""

from __future__ import annotations

from typing import Any

from runtime.daemon.worktree import Worktree, WorktreeError


def capture(worktree: Worktree, payload: dict[str, Any]) -> dict[str, Any]:
    raise WorktreeError(
        "unsupported_capability", "change capture not available in this runtime build"
    )


def apply(worktree: Worktree, payload: dict[str, Any]) -> dict[str, Any]:
    raise WorktreeError(
        "unsupported_capability", "change apply not available in this runtime build"
    )
