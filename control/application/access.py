"""Authorization helpers: ownership is checked on every global-ID lookup."""

from __future__ import annotations

from typing import Any

from control.domain.errors import DomainError, not_found
from control.domain.identity import Principal


def require_workspace(principal: Principal, workspace_id: str) -> None:
    if not principal.owns(workspace_id):
        raise not_found("workspace")


def require_scope(principal: Principal, scope: str) -> None:
    if not principal.can(scope):
        raise DomainError("forbidden", f"credential lacks scope {scope}")


def owned(
    uow: Any,
    principal: Principal,
    table: str,
    id: str,
    *,
    lock: bool = False,
    what: str | None = None,
) -> dict[str, Any]:
    row = uow.get(table, id, workspace_ids=principal.workspace_ids, lock=lock)
    if row is None:
        raise not_found(what or table.rstrip("s"))
    return row
