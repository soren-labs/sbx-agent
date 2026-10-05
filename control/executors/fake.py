"""Fake executor backend — deterministic allocation lifecycle for tests.

Returns scripted handles without spawning compute; pair with an in-process
RuntimePool stub when the test does not need a real daemon.
"""

from __future__ import annotations

from .port import AllocationSpec, ExecutorCapabilities, ExecutorError, ExecutorHandle


class FakeExecutorBackend:
    backend = "fake"

    def __init__(self) -> None:
        self.allocations: dict[str, ExecutorHandle] = {}
        self.terminated: list[str] = []
        self.fail_next: ExecutorError | None = None

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(runtime_transport="tcp")

    def allocate(self, spec: AllocationSpec, operation_id: str) -> ExecutorHandle:
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        existing = self.allocations.get(operation_id)
        if existing is not None:
            return existing
        handle = ExecutorHandle(
            backend=self.backend,
            handle_id=f"fake-{operation_id}",
            metadata={"spec": {"lease_id": spec.lease_id, "session_id": spec.session_id}},
        )
        self.allocations[operation_id] = handle
        return handle

    def lookup(self, operation_id: str) -> ExecutorHandle | None:
        return self.allocations.get(operation_id)

    def describe(self, handle: ExecutorHandle) -> dict:
        return {"alive": handle.handle_id not in self.terminated}

    def connect_runtime(self, handle: ExecutorHandle) -> dict:
        return {"endpoint": "tcp://fake", "transport": "tcp"}

    def capture_filesystem(self, handle: ExecutorHandle, prepared_manifest: dict) -> dict:
        raise ExecutorError("snapshot_unsupported", "fake backend has no snapshots")

    def restore(
        self, spec: AllocationSpec, snapshot_ref: dict, operation_id: str
    ) -> ExecutorHandle:
        raise ExecutorError("snapshot_unsupported", "fake backend has no snapshots")

    def terminate(self, handle: ExecutorHandle, operation_id: str) -> None:
        self.terminated.append(handle.handle_id)
        self.allocations.pop(str(handle.metadata.get("allocation_operation_id", "")), None)
