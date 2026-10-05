"""Runtime wire v1. Infrastructure records; no business state reducer."""

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field


class ProtocolError(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class OperationFrame(BaseModel):
    protocol_major: int = 1
    schema_version: int = 1
    operation_id: str
    operation_kind: str
    session_id: str
    lease_id: str
    lease_generation: int
    resource_fence: int
    grant_id: str
    grant_expires_at: float
    request_digest: str
    payload: dict[str, Any] = Field(default_factory=dict)

    def validate_body(self):
        if self.protocol_major != 1 or self.schema_version != 1:
            raise ProtocolError("runtime_incompatible")
        if digest(self.payload) != self.request_digest:
            raise ProtocolError("idempotency_conflict")
