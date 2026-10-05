"""Stable sbx-runtime wire protocol — frame vocabulary (RFC 167 §03).

Wire major starts at 1; minor additions are capability-negotiated. The SAME
frame vocabulary runs over loopback JSONL (Local executor) and authenticated
TLS WebSocket (deployed ingress); transports never change operation
semantics. A version mismatch MUST reject the handshake — never fall back to
shell execution.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field

PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
PAYLOAD_SCHEMA_VERSION = 1

FRAME = "frame"
FRAME_HELLO = "hello"
FRAME_HELLO_ACCEPTED = "hello.accepted"
FRAME_HELLO_REJECTED = "hello.rejected"
FRAME_OPERATION_SUBMIT = "operation.submit"
FRAME_OPERATION_ACCEPTED = "operation.accepted"
FRAME_OPERATION_REJECTED = "operation.rejected"
FRAME_OPERATION_STATUS = "operation.status"
FRAME_OPERATION_RESULT = "operation.result"
FRAME_EVENTS_BATCH = "events.batch"
FRAME_EVENTS_ACK = "events.ack"
FRAME_HEALTH_REPORT = "health.report"
FRAME_LEASE_REVOKED = "lease.revoked"
FRAME_PING = "ping"
FRAME_PONG = "pong"
FRAME_ERROR = "error"


class OperationKind(enum.StrEnum):
    ENVIRONMENT_PREPARE = "environment.prepare"
    WORKTREE_RESTORE = "worktree.restore"
    TURN_START = "turn.start"
    TURN_RESUME = "turn.resume"
    TURN_CANCEL = "turn.cancel"
    TURN_STEER = "turn.steer"
    APPROVAL_RESPOND = "approval.respond"
    FILES_LIST = "files.list"
    FILES_READ = "files.read"
    FILES_WRITE = "files.write"
    FILES_UPLOAD = "files.upload"
    CHANGES_OBSERVE = "changes.observe"
    CHANGES_CAPTURE = "changes.capture"
    CHANGES_APPLY = "changes.apply"
    CHANGES_EXPORT = "changes.export"
    TERMINAL_CREATE = "terminal.create"
    TERMINAL_INPUT = "terminal.input"
    TERMINAL_CLOSE = "terminal.close"
    SERVICE_ENSURE = "service.ensure"
    SERVICE_STOP = "service.stop"
    SNAPSHOT_PREPARE = "snapshot.prepare"
    SNAPSHOT_SEAL = "snapshot.seal"
    SNAPSHOT_ABORT = "snapshot.abort"
    WORKTREE_QUIESCE = "worktree.quiesce"
    WORKTREE_RELEASE = "worktree.release"
    RUNTIME_SHUTDOWN = "runtime.shutdown"
    CREDENTIAL_WRITEBACK = "credential.writeback"


class OperationState(enum.StrEnum):
    ACCEPTED = "accepted"
    STARTING = "starting"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


OPERATION_TERMINAL: frozenset[OperationState] = frozenset(
    {
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.INTERRUPTED,
        OperationState.UNKNOWN,
    }
)


def request_digest(payload: dict) -> str:
    """Canonical request digest over the operation payload (body identity)."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def mint_enrollment_token() -> str:
    return "enr_" + secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


def check_token(token: str, digest: str) -> bool:
    return hmac.compare_digest(token_digest(token), digest)


@dataclass(frozen=True)
class OperationEnvelope:
    """Mutating frame identity + fencing context (RFC 167 §03).

    Same ``operation_id`` + same ``request_digest`` MUST return the same
    durable status; same id with a different body MUST conflict.
    """

    operation_id: str
    operation_kind: OperationKind
    session_id: str
    lease_id: str
    lease_generation: int
    payload: dict = field(default_factory=dict)
    fence_generation: int | None = None
    grant_id: str | None = None
    grant_expires_at: float | None = None
    payload_schema_version: int = PAYLOAD_SCHEMA_VERSION
    request_digest: str = ""

    def __post_init__(self) -> None:
        if not self.request_digest:
            object.__setattr__(self, "request_digest", request_digest(self.payload))

    def to_dict(self) -> dict:
        return {
            "operation_id": self.operation_id,
            "operation_kind": self.operation_kind.value,
            "session_id": self.session_id,
            "lease_id": self.lease_id,
            "lease_generation": self.lease_generation,
            "fence_generation": self.fence_generation,
            "grant_id": self.grant_id,
            "grant_expires_at": self.grant_expires_at,
            "payload_schema_version": self.payload_schema_version,
            "request_digest": self.request_digest,
            "payload": self.payload,
        }

    @staticmethod
    def from_frame(raw: dict) -> OperationEnvelope:
        return OperationEnvelope(
            operation_id=str(raw["operation_id"]),
            operation_kind=OperationKind(raw["operation_kind"]),
            session_id=str(raw["session_id"]),
            lease_id=str(raw["lease_id"]),
            lease_generation=int(raw["lease_generation"]),
            fence_generation=raw.get("fence_generation"),
            grant_id=raw.get("grant_id"),
            grant_expires_at=raw.get("grant_expires_at"),
            payload_schema_version=int(raw.get("payload_schema_version") or PAYLOAD_SCHEMA_VERSION),
            request_digest=str(raw.get("request_digest") or ""),
            payload=dict(raw.get("payload") or {}),
        )


def make_frame(kind: str, **fields) -> dict:
    out = {FRAME: kind}
    out.update(fields)
    return out


def encode_frame(frame: dict) -> bytes:
    return (json.dumps(frame, separators=(",", ":")) + "\n").encode()


def decode_frame(line: bytes) -> dict:
    obj = json.loads(line.decode())
    if not isinstance(obj, dict) or FRAME not in obj:
        raise ValueError("malformed frame")
    return obj


def protocol_compatible(reported_major: int, reported_minor: int) -> bool:
    """Daemon reports its protocol range; control accepts same major only."""
    return reported_major == PROTOCOL_MAJOR


def hello_frame(
    *,
    lease_id: str,
    lease_generation: int,
    runtime_epoch: str,
    token: str,
    image_digest: str | None,
    runtime_build: str,
    harness_manifests: list[dict],
    recovered_operations: list[str],
    spool_watermark: int,
    health: dict,
) -> dict:
    return make_frame(
        FRAME_HELLO,
        lease_id=lease_id,
        lease_generation=lease_generation,
        runtime_epoch=runtime_epoch,
        enrollment_token=token,
        protocol={"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR},
        image_digest=image_digest,
        runtime_build=runtime_build,
        harness_manifests=harness_manifests,
        recovered_operations=recovered_operations,
        spool_watermark=spool_watermark,
        health=health,
        sent_at=time.time(),
    )
