"""Execution Job kinds -> ExecutionService commands."""

from __future__ import annotations

from typing import Any


def handlers(execution: Any) -> dict[str, Any]:
    return {
        "turn.dispatch": execution.handle_dispatch,
        "execution.reconcile": execution.handle_reconcile,
        "executor.allocate": execution.handle_allocate,
        "executor.release": execution.handle_release,
    }
