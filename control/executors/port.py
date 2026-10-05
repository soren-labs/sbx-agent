"""The ExecutorBackend port (RFC 167 §03).

Backends own allocate/lookup/describe/terminate, image boot, runtime
connectivity and supported snapshot facilities. They never select models,
normalize events or perform Git delivery. Generic ``exec`` is deliberately
absent — provider Turns/files/services go through runtime operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class AllocationSpec:
    """Everything needed to place one ExecutorLease."""

    workspace_id: str
    session_id: str
    lease_id: str
    allocation_effect_id: str
    lease_generation: int
    image_digest: str | None = None
    resource_class: str = "default"
    network_policy: str = "default"
    protocol_range: tuple[int, int] = (1, 1)  # supported protocol majors
    enrollment_ref: str = ""  # ingress endpoint the daemon dials
    connection: dict = field(default_factory=dict)  # selected compute Connection ref
    env: dict = field(default_factory=dict)
    tags: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutorHandle:
    """Opaque backend handle — never Session identity."""

    backend: str
    handle_id: str
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutorCapabilities:
    runtime_transport: str = "tcp"  # tcp|wss
    filesystem_snapshot: bool = False
    pause_restore: bool = False
    restore: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class ExecutorError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ExecutorBackend(Protocol):
    def capabilities(self) -> ExecutorCapabilities: ...

    def allocate(self, spec: AllocationSpec, operation_id: str) -> ExecutorHandle: ...

    def lookup(self, operation_id: str) -> ExecutorHandle | None:
        """Adopt a prior allocation by its operation tag after a crash.

        MUST NOT allocate a second runtime just because a response was lost."""
        ...

    def describe(self, handle: ExecutorHandle) -> dict: ...

    def connect_runtime(self, handle: ExecutorHandle) -> dict:
        """Endpoint/facility facts for the lease's runtime channel."""
        ...

    def capture_filesystem(self, handle: ExecutorHandle, prepared_manifest: dict) -> dict:
        """Backend-native snapshot where supported; returns a snapshot ref."""
        ...

    def restore(
        self, spec: AllocationSpec, snapshot_ref: dict, operation_id: str
    ) -> ExecutorHandle: ...

    def terminate(self, handle: ExecutorHandle, operation_id: str) -> None: ...
