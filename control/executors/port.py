from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class AllocationSpec:
    workspace_id: str
    session_id: str
    lease_id: str
    generation: int
    image_digest: str
    runtime_token: str = field(repr=False)
    resource_class: str = "standard"
    network_policy: str = "outbound"
    connection_id: str | None = None
    credential_id: str | None = None


class ExecutorBackend(Protocol):
    def capabilities(self) -> dict: ...
    def allocate(self, spec: AllocationSpec, operation_id: str) -> str: ...
    def lookup(self, operation_id: str) -> str | None: ...
    def describe(self, handle: str) -> dict: ...
    def connect_runtime(self, handle: str): ...
    def capture_filesystem(self, handle: str, prepared_manifest: dict) -> dict: ...
    def restore(self, spec: AllocationSpec, snapshot_ref: str, operation_id: str) -> str: ...
    def terminate(self, handle: str, operation_id: str) -> None: ...
