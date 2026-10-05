"""Enrollment token authority on the daemon side (RFC 167 §03).

The daemon presents its one-use token inside ``hello``; the control plane
verifies the digest against the lease record — the daemon itself never
stores credential secrets.
"""

from __future__ import annotations

import os


def enrollment_token_from_env(env_name: str = "SBX_ENROLLMENT_TOKEN") -> str:
    token = os.environ.get(env_name, "")
    if not token:
        raise SystemExit(f"missing enrollment token env {env_name}")
    return token
