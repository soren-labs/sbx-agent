"""Canonical error vocabulary (RFC 08 error shape, RFC 02 distinct reasons)."""

from __future__ import annotations

# code -> (category, http status, retryable default)
ERROR_CODES: dict[str, tuple[str, int, bool]] = {
    "not_found": ("access", 404, False),
    "forbidden": ("access", 403, False),
    "unauthenticated": ("authentication", 401, False),
    "csrf_failed": ("authentication", 403, False),
    "rate_limited": ("throttle", 429, True),
    "validation_failed": ("request", 422, False),
    "version_conflict": ("concurrency", 409, True),
    "idempotency_conflict": ("request", 409, False),
    "invalid_transition": ("state", 409, False),
    "unsupported_capability": ("capability", 422, False),
    "credential_invalid": ("credential", 409, False),
    "connection_revoked": ("credential", 409, False),
    "connection_in_use": ("credential", 409, False),
    "connection_required": ("credential", 409, False),
    "waiting_capacity": ("capacity", 409, True),
    "quota_exhausted": ("capacity", 429, False),
    "runtime_incompatible": ("runtime", 409, False),
    "executor_unavailable": ("runtime", 409, True),
    "context_unavailable": ("context", 409, False),
    "context_mismatch": ("context", 409, False),
    "outcome_unknown": ("outcome", 409, False),
    "output_contract_invalid": ("outcome", 422, False),
    "capture_failed": ("changes", 409, True),
    "stale_subject": ("changes", 409, False),
    "remote_head_changed": ("delivery", 409, False),
    "delivery_unresolved": ("delivery", 409, True),
    "gate_blocked": ("delivery", 409, False),
    "history_reset_required": ("events", 409, False),
    "invalid_cursor": ("events", 400, False),
    "stale_fence": ("fencing", 409, False),
    "operation_conflict": ("runtime", 409, False),
    "internal_error": ("platform", 500, True),
}


def category_of(code: str) -> str:
    return ERROR_CODES.get(code, ("platform", 500, False))[0]


def status_of(code: str) -> int:
    return ERROR_CODES.get(code, ("platform", 500, False))[1]


def retryable_default(code: str) -> bool:
    return ERROR_CODES.get(code, ("platform", 500, False))[2]
