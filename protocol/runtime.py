"""sbx-runtime wire protocol v1 (RFC 03 Stable runtime protocol).

Data/wire format only: versions, frame/operation vocabulary, request digests and
the lease-bound grant codec. No business decisions or secret storage.

Transport note (documented deviation): frames travel as authenticated JSON over
HTTPS request/response with the control plane as client (Modal encrypted tunnel,
or loopback for Local), instead of an executor-initiated WebSocket. Operation,
fence, dedupe and committed-ack semantics are identical.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
PROTOCOL_VERSION = f"{PROTOCOL_MAJOR}.{PROTOCOL_MINOR}"
PAYLOAD_SCHEMA_VERSION = 1

OPERATION_KINDS = frozenset(
    {
        "worktree.restore",
        "turn.start",
        "turn.cancel",
        "files.write",
        "changes.capture",
        "changes.apply",
        "check.run",
        "snapshot.prepare",
        "service.ensure",
        "service.stop",
        "terminal.create",
        "terminal.input",
        "terminal.close",
        "lease.renew",
        "runtime.shutdown",
    }
)
QUERY_KINDS = frozenset(
    {
        "operation.status",
        "files.list",
        "files.read",
        "changes.observe",
        "service.status",
        "service.logs",
        "terminal.read",
        "health.report",
    }
)
OPERATION_STATUSES = ("accepted", "starting", "started", "succeeded", "failed", "lost")
GRANT_SCOPES = frozenset({"manage", "read", "ingest"})


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def request_digest(kind: str, payload: dict[str, Any]) -> str:
    """Digest of the operation body; secret material is excluded by the caller."""
    return "sha256:" + hashlib.sha256(_canonical({"kind": kind, "payload": payload})).hexdigest()


def compatible(remote_major: int) -> bool:
    return remote_major == PROTOCOL_MAJOR


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encode_grant(key: bytes, claims: dict[str, Any]) -> str:
    body = _b64(_canonical(claims))
    sig = _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def decode_grant(key: bytes, token: str, *, now: float | None = None) -> dict[str, Any]:
    """Verify signature and expiry; raises ValueError on any failure."""
    try:
        body, sig = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("malformed grant") from exc
    expected = _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        raise ValueError("bad grant signature")
    claims = json.loads(_unb64(body))
    if float(claims.get("exp", 0)) < (now or time.time()):
        raise ValueError("grant expired")
    if claims.get("scope") not in GRANT_SCOPES:
        raise ValueError("bad grant scope")
    return claims
