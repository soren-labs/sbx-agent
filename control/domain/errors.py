"""Domain error vocabulary.

``DomainError.code`` is the canonical API error code (RFC 167 §08); the API
layer maps categories/status, never the other way around.
"""

from __future__ import annotations

ERROR_CODES: dict[str, str] = {
    # (code -> category)
    "not_found": "not_found",
    "forbidden": "forbidden",
    "unauthenticated": "forbidden",
    "version_conflict": "conflict",
    "idempotency_conflict": "conflict",
    "unsupported_capability": "validation",
    "credential_invalid": "credential",
    "connection_revoked": "credential",
    "waiting_capacity": "capacity",
    "rate_limited": "capacity",
    "quota_exhausted": "capacity",
    "runtime_incompatible": "runtime",
    "executor_unavailable": "runtime",
    "context_unavailable": "runtime",
    "context_mismatch": "runtime",
    "outcome_unknown": "outcome",
    "output_contract_invalid": "outcome",
    "capture_failed": "outcome",
    "stale_subject": "delivery",
    "remote_head_changed": "delivery",
    "delivery_unresolved": "delivery",
    "history_reset_required": "conflict",
    "invalid_cursor": "validation",
    "invalid_state": "conflict",
    "validation_failed": "validation",
    "conflict": "conflict",
    "internal": "internal",
}


class DomainError(Exception):
    """A safe, categorized domain failure. ``details`` must never hold secrets."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
        action: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        self.code = code
        self.category = ERROR_CODES[code]
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after
        self.action = action
        self.details = details or {}

    def to_dict(self, request_id: str | None = None) -> dict:
        body: dict = {
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        if self.action:
            body["action"] = self.action
        if request_id:
            body["request_id"] = request_id
        return {"error": body}


class NotFound(DomainError):
    def __init__(self, what: str, ident: str | None = None) -> None:
        suffix = f" {ident}" if ident else ""
        super().__init__("not_found", f"{what}{suffix} not found")


class Forbidden(DomainError):
    def __init__(self, message: str = "access denied") -> None:
        super().__init__("forbidden", message)


class InvalidTransition(DomainError):
    def __init__(self, entity: str, current: str, target: str) -> None:
        super().__init__(
            "invalid_state",
            f"{entity} cannot transition {current} -> {target}",
            details={"entity": entity, "current": current, "target": target},
        )


class TerminalViolation(DomainError):
    def __init__(self, entity: str, state: str) -> None:
        super().__init__(
            "invalid_state",
            f"{entity} is terminal ({state}); mutation is forbidden",
            details={"entity": entity, "state": state},
        )
