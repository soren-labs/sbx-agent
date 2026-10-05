"""Unified /api error contract (RFC 167 §08).

Every error response is ``error{code,category,message,retryable,
retry_after?,action?,details,request_id}`` with safe details. Domain codes
map to HTTP status + category here — auth, provider outcome and transport
failure stay distinguishable.
"""

from __future__ import annotations

from typing import Any

from control.domain.errors import DomainError

# code -> (http status, category, retryable)
_CODE_MAP: dict[str, tuple[int, str, bool]] = {
    "not_found": (404, "resource", False),
    "forbidden": (403, "authorization", False),
    "unauthenticated": (401, "authentication", False),
    "validation_failed": (422, "request", False),
    "invalid_state": (409, "state", False),
    "version_conflict": (409, "concurrency", True),
    "idempotency_conflict": (409, "concurrency", False),
    "rate_limited": (429, "rate", True),
    "quota_exhausted": (429, "rate", True),
    "credential_invalid": (422, "credential", False),
    "connection_revoked": (409, "credential", False),
    "waiting_capacity": (503, "capacity", True),
    "executor_unavailable": (503, "executor", True),
    "context_unavailable": (409, "context", True),
    "context_mismatch": (409, "context", False),
    "outcome_unknown": (409, "outcome", True),
    "output_contract_invalid": (422, "contract", False),
    "capture_failed": (422, "changeset", True),
    "stale_subject": (409, "delivery", False),
    "remote_head_changed": (409, "delivery", True),
    "delivery_unresolved": (409, "delivery", False),
    "history_reset_required": (409, "stream", False),
    "invalid_cursor": (422, "request", False),
    "unsupported_capability": (422, "capability", False),
    "capability_unsupported": (422, "capability", False),
    "runtime_incompatible": (422, "capability", False),
}


def error_payload(
    *,
    code: str,
    message: str,
    request_id: str,
    category: str = "internal",
    retryable: bool = False,
    retry_after: float | None = None,
    action: str | None = None,
    details: Any = None,
) -> dict:
    body = {
        "code": code,
        "category": category,
        "message": message,
        "retryable": retryable,
        "details": details or {},
        "request_id": request_id,
    }
    if retry_after is not None:
        body["retry_after"] = retry_after
    if action:
        body["action"] = action
    return {"error": body}


class ApiError(Exception):
    """HTTP-level error raised inside the /api surface."""

    def __init__(
        self,
        status: int,
        *,
        code: str,
        category: str,
        message: str,
        retryable: bool = False,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.category = category
        self.message = message
        self.retryable = retryable
        self.details = details


def from_domain(exc: DomainError) -> ApiError:
    status, category, retryable = _CODE_MAP.get(exc.code, (500, "internal", True))
    return ApiError(
        status,
        code=exc.code,
        category=category,
        message=str(exc),
        retryable=retryable,
        details=getattr(exc, "details", None),
    )


def unsupported(feature: str) -> ApiError:
    """Truthful capability response — never a fake success."""
    return ApiError(
        422,
        code="unsupported_capability",
        category="capability",
        message=f"{feature} is not implemented in this build",
        retryable=False,
    )
