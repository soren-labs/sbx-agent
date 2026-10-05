"""Product identity values: Principal, email normalization, password policy."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from control.domain.errors import DomainError

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 512


@dataclass(frozen=True)
class Principal:
    """Resolved identical for cookie and API-key authentication (RFC 06)."""

    user_id: str
    workspace_ids: tuple[str, ...]
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({"*"}))
    auth_epoch: int = 1
    via: str = "cookie"
    credential_id: str | None = None

    def can(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes

    def owns(self, workspace_id: str) -> bool:
        return workspace_id in self.workspace_ids

    @property
    def default_workspace_id(self) -> str:
        return self.workspace_ids[0]


def normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise DomainError("validation_failed", "invalid email address", details={"field": "email"})
    return email


def check_password(password: str) -> None:
    if not isinstance(password, str) or not (
        MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH
    ):
        raise DomainError(
            "validation_failed",
            f"password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters",
            details={"field": "password"},
        )
