"""Modal executor backend (RFC 167 §03).

Creates one Modal Sandbox per lease running ``python -m runtime.daemon.main``
whose daemon connects outbound to the ingress endpoint recorded on the
AllocationSpec. ``import modal`` is lazy — collecting tests never opens a
connection. Modal credentials stay in the executor worker: they arrive via
``spec.connection`` (a decrypted compute credential env map) or process
env, never via the business API.
"""

from __future__ import annotations

import contextlib
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

# The sandbox cannot reach a control-plane loopback, so the runtime SERVES
# on this container port and the control plane dials in over the sandbox's
# encrypted Modal tunnel.
SERVE_PORT = 8443


def _load_modal() -> Any:
    import modal

    return modal


@contextlib.contextmanager
def _modal_client_scope(modal: Any, lock: Any, creds: tuple[str, str] | None):
    """Bind a per-call credential as the modal env-client under ``lock``."""
    if creds is None:
        # Deployment-level ambient profile — only reached when the backend
        # itself was configured with credentials, never as a silent fallback
        # inside a user dispatch (spec.connection would have carried them).
        with lock:
            yield modal
        return
    client = modal.client.Client.from_credentials(creds[0], creds[1])
    with lock:
        prev = getattr(modal.client.Client, "_client_from_env", None)
        modal.client.Client.set_env_client(client)
        try:
            yield modal
        finally:
            modal.client.Client.set_env_client(prev)


class ModalExecutorBackend:
    backend = "modal"

    def __init__(
        self,
        *,
        default_endpoint: str | None = None,
        image: Any = None,
        image_name: str | None = "sbx-runtime-opencode",
        app_name: str = "sbx-runtime",
        token_id: str | None = None,
        token_secret: str | None = None,
        timeout_seconds: int = 60 * 60,
    ) -> None:
        import threading

        self._endpoint = default_endpoint
        self._image = image
        self._image_name = image_name
        self._app_name = app_name
        self._token_id = token_id
        self._token_secret = token_secret
        self._timeout = timeout_seconds
        # Serializes the modal client's global env-client slot; the client
        # itself is rebuilt per call from the caller's credential material.
        self._client_lock = threading.Lock()

    def _credentials(self, connection: dict | None) -> tuple[str, str] | None:
        """Per-call compute creds: spec.connection env → ctor pair → ambient.

        ``spec.connection`` carries the decrypted compute Connection env map
        (``MODAL_TOKEN_ID``/``MODAL_TOKEN_SECRET``) minted for this call —
        the USER's stored credential, not an ambient profile.
        """
        cenv = (connection or {}).get("env") or {}
        tid = cenv.get("MODAL_TOKEN_ID") or self._token_id
        ts = cenv.get("MODAL_TOKEN_SECRET") or self._token_secret
        if tid and ts:
            return (str(tid), str(ts))
        return None

    def _locked_client(self, connection: dict | None):
        """Context manager yielding ``modal`` with the caller's credential
        bound as the env-client for the call's duration.

        Modal's SDK resolves its stub from the process-wide env client; we
        hold ``_client_lock`` for the whole call so a concurrent allocate for
        another owner can never observe the wrong credential.
        """
        creds = self._credentials(connection)
        modal = _load_modal()
        return _modal_client_scope(modal, self._client_lock, creds)

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            runtime_transport="tcp",
            filesystem_snapshot=True,  # Sandbox.snapshot_filesystem
            pause_restore=False,
            restore=False,
        )

    # -- port ---------------------------------------------------------

    def allocate(
        self,
        spec: AllocationSpec,
        operation_id: str,
        *,
        connection: dict | None = None,
    ) -> ExecutorHandle:
        with self._locked_client(connection or spec.connection) as modal:
            existing = self._lookup(modal, operation_id)
            if existing is not None:
                return existing  # adopt: never double-allocate on a lost response
            env = {
                "SBX_ENROLLMENT_TOKEN": spec.env.get("SBX_ENROLLMENT_TOKEN", ""),
            }
            for key, value in (spec.env or {}).items():
                env.setdefault(key, value)
            argv = [
                *_DAEMON_COMMAND,
                "--lease-id",
                spec.lease_id,
                "--lease-generation",
                str(spec.lease_generation),
                "--serve",
                str(SERVE_PORT),
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
                "encrypted_ports": [SERVE_PORT],
            }
            sandbox = self._sandbox_create(modal, argv, kwargs)
            tunnel = (sandbox.tunnels() or {}).get(SERVE_PORT)
            dial_url = None
            if tunnel is not None:
                host, port = tunnel.tls_socket
                dial_url = f"tls://{host}:{port}"
            return ExecutorHandle(
                backend=self.backend,
                handle_id=sandbox.object_id,
                metadata={
                    "sandbox_id": sandbox.object_id,
                    "dial_url": dial_url,
                    "tags": tags,
                    "allocation_operation_id": operation_id,
                },
            )

    def lookup(self, operation_id: str, *, connection: dict | None = None) -> ExecutorHandle | None:
        with self._locked_client(connection) as modal:
            return self._lookup(modal, operation_id)

    def _lookup(self, modal: Any, operation_id: str) -> ExecutorHandle | None:
        try:
            candidates = list(modal.Sandbox.list(tags={TAG_OPERATION: operation_id}))
        except Exception as exc:
            raise ExecutorError("lookup_failed", str(exc), retryable=True) from exc
        if not candidates:
            return None
        sandbox = candidates[0]
        tags = dict(sandbox.tags) if getattr(sandbox, "tags", None) else {}
        dial_url = None
        try:
            tunnel = (sandbox.tunnels() or {}).get(SERVE_PORT)
            if tunnel is not None:
                host, port = tunnel.tls_socket
                dial_url = f"tls://{host}:{port}"
        except Exception:
            dial_url = None
        return ExecutorHandle(
            backend=self.backend,
            handle_id=sandbox.object_id,
            metadata={
                "sandbox_id": sandbox.object_id,
                "dial_url": dial_url,
                "tags": tags,
                "allocation_operation_id": operation_id,
            },
        )

    def describe(self, handle: ExecutorHandle, *, connection: dict | None = None) -> dict:
        with self._locked_client(connection) as modal:
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

    def capture_filesystem(
        self,
        handle: ExecutorHandle,
        prepared_manifest: dict,
        *,
        connection: dict | None = None,
    ) -> dict:
        with self._locked_client(connection) as modal:
            sandbox = modal.Sandbox.from_id(handle.metadata["sandbox_id"])
            image = sandbox.snapshot_filesystem()
        return {
            "kind": "modal_filesystem",
            "image_id": image.object_id,
            "manifest_digest": prepared_manifest.get("digest"),
        }

    def restore(
        self,
        spec: AllocationSpec,
        snapshot_ref: dict,
        operation_id: str,
        *,
        connection: dict | None = None,
    ) -> ExecutorHandle:
        if snapshot_ref.get("kind") != "modal_filesystem":
            raise ExecutorError("snapshot_unsupported", "modal restore needs modal_filesystem ref")
        with self._locked_client(connection or spec.connection) as modal:
            self._image_override = modal.Image.from_id(snapshot_ref["image_id"])
            try:
                return self.allocate(spec, operation_id, connection=connection)
            finally:
                self._image_override = None

    def terminate(
        self,
        handle: ExecutorHandle,
        operation_id: str,
        *,
        connection: dict | None = None,
    ) -> None:
        with self._locked_client(connection) as modal:
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
        if self._image_name:
            # Named image carries /opt/sbx/runtime + the pinned opencode CLI
            # (built by `python -m runtime.image --provider opencode`).
            return modal.Image.from_name(self._image_name)
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
