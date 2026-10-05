"""Product identity: User, Workspace, Principal (RFC 167 §02/§06).

Product auth is email/password with verified email, secure cookie login
sessions and scoped hashed API keys. Cookie and API-key auth resolve the same
Principal shape.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class MembershipRole(enum.StrEnum):
    OWNER = "owner"
    MEMBER = "member"


class MembershipStatus(enum.StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


@dataclass(frozen=True)
class Principal:
    """Resolved caller identity. ``auth_epoch`` invalidates stale grants."""

    user_id: str
    workspace_ids: frozenset[str]
    scopes: frozenset[str]
    auth_epoch: int = 1
    session_id: str | None = None  # when acting as a Session tool
    label: str = "user"

    def workspaces(self) -> frozenset[str]:
        return self.workspace_ids

    def scoped_workspaces(self) -> frozenset[str]:
        """Workspace IDs this principal may act within (superset when session tool)."""
        return self.workspace_ids

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes or "*" in self.scopes


@dataclass
class User:
    id: str
    email_normalized: str
    verified_at: object | None
    identity_version: int
    display_name: str | None = None
    created_at: object = None


@dataclass
class Workspace:
    id: str
    owner_user_id: str
    name: str
    policy_version: int = 1
    created_at: object = None


def normalize_email(email: str) -> str:
    local, _, domain = email.strip().partition("@")
    if not local or not domain or "@" in domain:
        from .errors import DomainError

        raise DomainError("validation_failed", "invalid email address")
    return f"{local.lower()}@{domain.lower()}"
