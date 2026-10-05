"""Wire error vocabulary shared by control plane and sbx-runtime.

Error codes are stable strings carried in frames; they are not exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Connection / handshake failures.
WIRE_PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
WIRE_ENROLLMENT_INVALID = "enrollment_invalid"
WIRE_ENROLLMENT_EXPIRED = "enrollment_expired"

# Operation acceptance failures (fsynced in the daemon journal).
WIRE_OPERATION_CONFLICT = "operation_conflict"
WIRE_FENCE_STALE = "fence_stale"
WIRE_GRANT_EXPIRED = "grant_expired"
WIRE_PAYLOAD_INVALID = "payload_invalid"
WIRE_PAYLOAD_SCHEMA_UNKNOWN = "payload_schema_unknown"
WIRE_PRECONDITION_FAILED = "precondition_failed"
WIRE_CAPABILITY_UNSUPPORTED = "capability_unsupported"
WIRE_RESOURCE_BUSY = "resource_busy"
WIRE_RESOURCE_UNAVAILABLE = "resource_unavailable"
WIRE_QUOTA_EXCEEDED = "quota_exceeded"

# Terminal evidence classification (Harness outcome kinds).
WIRE_CONTEXT_MISMATCH = "context_mismatch"
WIRE_CREDENTIAL_INVALID = "credential_invalid"
WIRE_RATE_LIMITED = "rate_limited"
WIRE_PROVIDER_ERROR = "provider_error"
WIRE_INTERRUPTED = "interrupted"
WIRE_PROCESS_ERROR = "process_error"

_RETRYABLE: frozenset[str] = frozenset(
    {
        WIRE_RATE_LIMITED,
        WIRE_RESOURCE_BUSY,
        WIRE_RESOURCE_UNAVAILABLE,
        WIRE_QUOTA_EXCEEDED,
    }
)


def retryable(code: str) -> bool:
    return code in _RETRYABLE


@dataclass(frozen=True)
class WireError:
    """Structured error carried inside frames and terminal evidence."""

    code: str
    message: str
    retry_advice: str = "none"  # none|retry|retry_later|reallocate|manual
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message, "retry_advice": self.retry_advice}
        if self.details:
            out["details"] = self.details
        return out

    @staticmethod
    def from_dict(raw: dict) -> WireError:
        return WireError(
            code=str(raw.get("code", "unknown")),
            message=str(raw.get("message", "")),
            retry_advice=str(raw.get("retry_advice", "none")),
            details=dict(raw.get("details") or {}),
        )
