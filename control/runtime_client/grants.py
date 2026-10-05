"""Lease-scoped enrollment keys and short-lived grants.

The per-lease key is derived from a control master key that never leaves the
control plane; only the derived key enters the executor. A restarted control
plane re-derives it, so no lease secret is stored in the database.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from protocol.runtime import encode_grant


def lease_key(master: bytes, lease_id: str, generation: int) -> bytes:
    return hmac.new(master, f"sbx-lease:{lease_id}:{generation}".encode(), hashlib.sha256).digest()


def mint(
    key: bytes, lease_id: str, generation: int, *, scope: str = "manage", ttl: float = 300
) -> str:
    claims = {
        "lease_id": lease_id,
        "generation": generation,
        "scope": scope,
        "exp": time.time() + ttl,
    }
    return encode_grant(key, claims)
