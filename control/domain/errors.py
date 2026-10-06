"""Domain error carrying the canonical public error vocabulary."""

from __future__ import annotations

from typing import Any

from protocol.errors import category_of, retryable_default, status_of


class DomainError(Exception):
    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
        retry_after: float | None = None,
        action: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code.replace("_", " ")
        self.details = details or {}
        self.category = category_of(code)
        self.retryable = retryable_default(code) if retryable is None else retryable
        self.retry_after = retry_after
        self.action = action
        self.status = status or status_of(code)

    def to_dict(self, request_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
            "request_id": request_id,
        }
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        if self.action:
            body["action"] = self.action
        return {"error": body}


def not_found(what: str = "resource") -> DomainError:
    # Cross-owner access is reported as absence (RFC 06 access table).
    return DomainError("not_found", f"{what} not found")
