"""Enrollment grants — one-use, lease-bound, short-lived (RFC 167 §03).

The control plane mints a token per lease allocation; only the digest is
stored (inside the lease's opaque handle metadata). ``hello`` presents the
token; verification is digest comparison plus expiry. Renewals require
current DB authority — this phase issues one grant per allocation.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from protocol.runtime import check_token, mint_enrollment_token, token_digest

DEFAULT_GRANT_TTL_S = 3600.0


@dataclass(frozen=True)
class EnrollmentGrant:
    grant_id: str
    lease_id: str
    token: str  # plaintext — handed to the daemon once, never persisted
    expires_at: float

    def digest(self) -> str:
        return token_digest(self.token)

    def to_handle_record(self) -> dict:
        return {
            "grant_id": self.grant_id,
            "digest": self.digest(),
            "expires_at": self.expires_at,
        }


def mint_grant(lease_id: str, ttl_seconds: float = DEFAULT_GRANT_TTL_S) -> EnrollmentGrant:
    return EnrollmentGrant(
        grant_id="grant_" + uuid.uuid4().hex,
        lease_id=lease_id,
        token=mint_enrollment_token(),
        expires_at=time.time() + ttl_seconds,
    )


def verify_grant(lease_handle: dict, token: str) -> bool:
    """Check ``token`` against the digest stored on the lease handle."""
    record = (lease_handle or {}).get("enrollment") or {}
    digest = record.get("digest")
    expires_at = record.get("expires_at")
    if not digest or not expires_at or float(expires_at) < time.time():
        return False
    return check_token(token, digest)


def grant_context(lease_handle: dict) -> tuple[str | None, float | None]:
    """grant_id / expiry for operation envelopes."""
    record = (lease_handle or {}).get("enrollment") or {}
    return record.get("grant_id"), record.get("expires_at")
