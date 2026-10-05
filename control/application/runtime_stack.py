"""Composition root — wires IngressServer + RuntimePool + ExecutionService
(RFC 167 §03 runtime plane inside the control-plane process).

The ingress verifies ``hello`` against committed lease rows (enrollment
digest + generation), commits runtime evidence via the ingest path, and
routes operation.result frames to the authoritative settle path.
"""

from __future__ import annotations

from typing import Any

from control.persistence.unit_of_work import SqlUnitOfWork
from control.runtime_client import grants
from control.runtime_client.client import RuntimePool
from control.runtime_client.ingress import IngressServer

from .execution import ExecutionService


class RuntimeStack:
    """Owns the runtime plane lifecycle for one process."""

    def __init__(
        self,
        db,
        backends: dict[str, Any],
        credential_resolver=None,
    ) -> None:
        self.db = db
        self.service = ExecutionService(
            db, backends=backends, pool=None, credential_resolver=credential_resolver
        )

        def verify_hello(hello: dict) -> dict | None:
            lease_id = str(hello.get("lease_id") or "")
            with SqlUnitOfWork(db, actor={"kind": "ingress"}) as uow:
                row = uow.leases.get(hello.get("workspace_id") or "", lease_id)
                if row is None:
                    # hello may omit workspace — look up by id
                    row = uow.rows.one("SELECT * FROM executor_leases WHERE id=%s", (lease_id,))
                if row is None or row["state"] not in ("allocating", "ready", "quiescing"):
                    return None
                token = str(hello.get("enrollment_token") or "")
                if not grants.verify_grant(row.get("handle") or {}, token):
                    return None
                if int(hello.get("lease_generation") or -1) != int(row["generation"]):
                    return None
                return dict(row)

        self.ingress = IngressServer(
            verify_hello=verify_hello,
            on_events=self.service.ingest_batch,
            on_result=self.service.on_operation_result,
            on_detach=self.service.handle_detach,
        )
        self.pool = RuntimePool(self.ingress)
        self.service.pool = self.pool

    def register_backend(self, kind: str, backend) -> None:
        self.service.backends[kind] = backend

    def start(self) -> None:
        self.pool.start()

    def stop(self) -> None:
        self.pool.stop()

    def __enter__(self) -> RuntimeStack:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
