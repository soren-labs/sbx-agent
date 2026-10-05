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
        git_resolver=None,
        compute_resolver=None,
    ) -> None:
        self.db = db
        self.service = ExecutionService(
            db,
            backends=backends,
            pool=None,
            credential_resolver=credential_resolver,
            git_resolver=git_resolver,
            compute_resolver=compute_resolver,
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

    # -- service wiring --------------------------------------------------

    def set_resolvers(
        self, *, credential_resolver=None, git_resolver=None, compute_resolver=None
    ) -> None:
        if credential_resolver is not None:
            self.service.credential_resolver = credential_resolver
        if git_resolver is not None:
            self.service.git_resolver = git_resolver
        if compute_resolver is not None:
            self.service.compute_resolver = compute_resolver

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


def make_connection_resolver(connection_service, db) -> object:
    """Vault-backed credential_resolver for ExecutionService: resolves the
    session's workspace inference connection into an isolated-HOME file
    bundle + env allowlist through a purpose-bound grant. Never falls back
    to ambient state — absent connection raises and the dispatch fails
    cleanly (RFC 167 §06)."""

    def resolve(session, turn, *, execution_id=None, lease_id=None, workspace_id=None):
        import json as _json

        from control.application.connections import (
            PURPOSE_RUNTIME_EXECUTION,
        )
        from control.persistence.unit_of_work import SqlUnitOfWork

        ws = workspace_id or session["workspace_id"]
        provider = (
            (turn.get("resolved_settings") or {}).get("provider_id")
            or ((session.get("resolved_settings") or {}).get("provider_id"))
            or session.get("provider_id")
            or "opencode"
        )
        kind = "opencode_zen" if provider == "opencode" else provider

        with SqlUnitOfWork(db, actor={"kind": "runtime", "id": "resolver"}) as uow:
            conn = connection_service.select_connection(
                uow, workspace_id=ws, kind=kind, purpose=PURPOSE_RUNTIME_EXECUTION
            )
            material = connection_service.materialize(
                uow,
                workspace_id=ws,
                connection_id=conn["id"],
                purpose=PURPOSE_RUNTIME_EXECUTION,
                session_id=session["id"],
                execution_id=execution_id,
                lease_id=lease_id,
            )
            uow.commit()

        payload = material.payload
        try:
            if conn["kind"] == "opencode_zen":
                return {
                    "files": {
                        ".local/share/opencode/auth.json": _json.dumps(
                            {"opencode": {"type": "api", "key": payload["api_key"]}}
                        )
                    },
                    "env": {},
                }
            if conn["kind"] == "codex":
                return {"files": dict(payload.get("files") or {}), "env": {}}
            return {"files": {}, "env": {}}
        finally:
            del material, payload

    return resolve


def make_git_resolver(connection_service, db) -> object:
    """Vault-backed resolver for worktree materialization: the workspace's
    git Connection becomes a short-lived env bundle (``GIT_AUTH_TOKEN``)
    bound to this dispatch's lease — used only for fetch inside the
    executor, never stored or logged."""

    def resolve(session, *, workspace_id=None, lease_id=None, **_kw):
        from control.application.connections import PURPOSE_GIT_CLONE
        from control.persistence.unit_of_work import SqlUnitOfWork

        ws = workspace_id or session["workspace_id"]
        with SqlUnitOfWork(db, actor={"kind": "runtime", "id": "git-resolver"}) as uow:
            conn = connection_service.select_connection(
                uow, workspace_id=ws, kind="github", purpose=PURPOSE_GIT_CLONE
            )
            material = connection_service.materialize(
                uow,
                workspace_id=ws,
                connection_id=conn["id"],
                purpose=PURPOSE_GIT_CLONE,
                session_id=session["id"],
                lease_id=lease_id,
            )
            uow.commit()

        payload = material.payload
        try:
            return {"env": {"GIT_AUTH_TOKEN": payload["token"]}}
        finally:
            del material, payload

    return resolve


def make_compute_resolver(connection_service, db) -> object:
    """Vault-backed resolver for the executor backend: the workspace's
    Modal Connection becomes ``spec.connection.env`` for exactly this
    allocate/terminate call (PURPOSE_EXECUTOR_WORKER)."""

    def resolve(session, *, workspace_id=None, lease_id=None, **_kw):
        from control.application.connections import PURPOSE_EXECUTOR_WORKER
        from control.persistence.unit_of_work import SqlUnitOfWork

        ws = workspace_id or session["workspace_id"]
        with SqlUnitOfWork(db, actor={"kind": "runtime", "id": "compute-resolver"}) as uow:
            conn = connection_service.select_connection(
                uow, workspace_id=ws, kind="modal", purpose=PURPOSE_EXECUTOR_WORKER
            )
            material = connection_service.materialize(
                uow,
                workspace_id=ws,
                connection_id=conn["id"],
                purpose=PURPOSE_EXECUTOR_WORKER,
                session_id=session["id"],
                lease_id=lease_id,
            )
            uow.commit()

        payload = material.payload
        try:
            return {
                "env": {
                    "MODAL_TOKEN_ID": payload["token_id"],
                    "MODAL_TOKEN_SECRET": payload["token_secret"],
                }
            }
        finally:
            del material, payload

    return resolve


def make_remote_factory(connection_service, db):
    """Delivery-plane remote factory: materializes the bound (or selected)
    github Connection's credential for exactly this delivery's use and
    returns a real GithubRemote — token lives only inside the remote."""
    from control.application.connections import PURPOSE_DELIVERY_WORKER
    from control.persistence.unit_of_work import SqlUnitOfWork
    from control.vcs.remote import GithubRemote

    def factory(delivery, connections=None):
        ws = delivery["workspace_id"]
        conn_id = delivery.get("connection_id")
        with SqlUnitOfWork(db, actor={"kind": "delivery", "id": "remote"}) as uow:
            if conn_id:
                conn = uow.connections.get(ws, conn_id)
            else:
                conn = connection_service.select_connection(
                    uow, workspace_id=ws, kind="github", purpose=PURPOSE_DELIVERY_WORKER
                )
            material = connection_service.materialize(
                uow,
                workspace_id=ws,
                connection_id=conn["id"],
                purpose=PURPOSE_DELIVERY_WORKER,
                session_id=delivery.get("session_id"),
            )
            uow.commit()
        return GithubRemote(material.payload["token"])

    return factory
