"""Modal executor backend (RFC 167 §03).

Creates one Modal Sandbox per lease running ``python -m runtime.daemon.main``
whose daemon connects outbound to the ingress endpoint recorded on the
AllocationSpec. ``import modal`` is lazy — collecting tests never opens a
connection. Modal credentials stay in the executor worker: they arrive via
``spec.connection`` (a decrypted compute credential env map) or process
env, never via the business API.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .port import (
    AllocationSpec,
    ExecutorCapabilities,
    ExecutorError,
    ExecutorHandle,
)

# Operation-tag keys used for adopt/terminate discovery (RFC 167 §03:
# "Discovery MUST use operation tags").
TAG_OPERATION = "sbx-operation"
TAG_SESSION = "sbx-session"
TAG_LEASE = "sbx-lease"

_DAEMON_COMMAND = [
    "python",
    "-m",
    "runtime.daemon.main",
]


def _load_modal() -> Any:
    import modal

    return modal


class ModalExecutorBackend:
    backend = "modal"

    def __init__(
        self,
        *,
        default_endpoint: str | None = None,
        image: Any = None,
        app_name: str = "sbx-runtime",
        token_id: str | None = None,
        token_secret: str | None = None,
        timeout_seconds: int = 60 * 60,
    ) -> None:
        self._endpoint = default_endpoint
        self._image = image
        self._app_name = app_name
        self._token_id = token_id
        self._token_secret = token_secret
        self._timeout = timeout_seconds

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            runtime_transport="tcp",
            filesystem_snapshot=True,  # Sandbox.snapshot_filesystem
            pause_restore=False,
            restore=False,
        )

    # -- port ---------------------------------------------------------

    def allocate(self, spec: AllocationSpec, operation_id: str) -> ExecutorHandle:
        existing = self.lookup(operation_id)
        if existing is not None:
            return existing  # adopt: never double-allocate on a lost response
        modal = _load_modal()
        env = {
            "SBX_ENROLLMENT_TOKEN": spec.env.get("SBX_ENROLLMENT_TOKEN", ""),
            "SBX_INGRESS": spec.enrollment_ref or self._endpoint or "",
        }
        for key, value in (spec.env or {}).items():
            env.setdefault(key, value)
        argv = [
            *_DAEMON_COMMAND,
            "--lease-id",
            spec.lease_id,
            "--lease-generation",
            str(spec.lease_generation),
            "--connect",
            env["SBX_INGRESS"],
            "--worktree",
            "/work/worktree",
            "--state",
            "/state",
        ]
        if spec.image_digest:
            argv += ["--image-digest", spec.image_digest]
        tags = {
            TAG_OPERATION: operation_id,
            TAG_SESSION: spec.session_id,
            TAG_LEASE: spec.lease_id,
        }
        kwargs: dict[str, Any] = {
            "image": self._resolve_image(modal),
            "env": env,
            "timeout": self._timeout,
            "tags": tags,
        }
        sandbox = self._sandbox_create(modal, argv, kwargs)
        return ExecutorHandle(
            backend=self.backend,
            handle_id=sandbox.object_id,
            metadata={
                "sandbox_id": sandbox.object_id,
                "endpoint": env["SBX_INGRESS"],
                "tags": tags,
                "allocation_operation_id": operation_id,
            },
        )

    def lookup(self, operation_id: str) -> ExecutorHandle | None:
        modal = _load_modal()
        try:
            candidates = list(modal.Sandbox.list(tags={TAG_OPERATION: operation_id}))
        except Exception as exc:
            raise ExecutorError("lookup_failed", str(exc), retryable=True) from exc
        if not candidates:
            return None
        sandbox = candidates[0]
        tags = dict(sandbox.tags) if getattr(sandbox, "tags", None) else {}
        return ExecutorHandle(
            backend=self.backend,
            handle_id=sandbox.object_id,
            metadata={
                "sandbox_id": sandbox.object_id,
                "tags": tags,
                "allocation_operation_id": operation_id,
            },
        )

    def describe(self, handle: ExecutorHandle) -> dict:
        modal = _load_modal()
        try:
            sandbox = modal.Sandbox.from_id(handle.metadata["sandbox_id"])
            alive = True
        except Exception:
            sandbox = None
            alive = False
        return {
            "alive": alive,
            "sandbox_id": handle.metadata["sandbox_id"],
            "poll": getattr(sandbox, "poll", lambda: None)(),
        }

    def connect_runtime(self, handle: ExecutorHandle) -> dict:
        return {
            "endpoint": handle.metadata.get("endpoint"),
            "transport": "tcp",
        }

    def capture_filesystem(self, handle: ExecutorHandle, prepared_manifest: dict) -> dict:
        modal = _load_modal()
        sandbox = modal.Sandbox.from_id(handle.metadata["sandbox_id"])
        image = sandbox.snapshot_filesystem()
        return {
            "kind": "modal_filesystem",
            "image_id": image.object_id,
            "manifest_digest": prepared_manifest.get("digest"),
        }

    def restore(
        self, spec: AllocationSpec, snapshot_ref: dict, operation_id: str
    ) -> ExecutorHandle:
        if snapshot_ref.get("kind") != "modal_filesystem":
            raise ExecutorError("snapshot_unsupported", "modal restore needs modal_filesystem ref")
        modal = _load_modal()
        self._image_override = modal.Image.from_id(snapshot_ref["image_id"])
        try:
            return self.allocate(spec, operation_id)
        finally:
            self._image_override = None

    def terminate(self, handle: ExecutorHandle, operation_id: str) -> None:
        modal = _load_modal()
        try:
            sandbox = modal.Sandbox.from_id(handle.metadata["sandbox_id"])
            sandbox.terminate()
        except Exception as exc:
            name = type(exc).__name__
            if name not in ("NotFoundError", "ConflictError"):
                raise

    # -- internals -------------------------------------------------------

    def _resolve_image(self, modal: Any) -> Any:
        override = getattr(self, "_image_override", None)
        if override is not None:
            return override
        if self._image is not None:
            return self._image
        return modal.Image.debian_slim().pip_install("websockets")

    def _sandbox_create(self, modal: Any, argv: list[str], kwargs: dict) -> Any:
        kwargs.setdefault("app", self._lookup_app(modal))
        return modal.Sandbox.create(*argv, **kwargs)

    def _lookup_app(self, modal: Any) -> Any:
        try:
            return modal.App.lookup(self._app_name, create_if_missing=True)
        except Exception as exc:
            raise ExecutorError("app_lookup_failed", str(exc), retryable=True) from exc


def build_spec_env(token: str) -> dict:
    """Helper: env for the sandbox carrying the enrollment token."""
    return {
        "SBX_ENROLLMENT_TOKEN": token,
        "SBX_BOOT_ID": uuid.uuid4().hex,
        "SBX_BOOT_TS": str(time.time()),
    }


def manifest_handle(handle: ExecutorHandle) -> str:
    return json.dumps({"sandbox_id": handle.handle_id, "backend": handle.backend})
