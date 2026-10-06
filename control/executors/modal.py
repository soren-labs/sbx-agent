"""Modal Executor backend using the owner's selected Modal Connection.

Modal credentials stay in this worker; the sandbox receives only the per-lease
enrollment key. The bounded boot command starts sbx-runtime; Turns/files use
runtime operations over the encrypted tunnel.

Allocation is idempotent by the allocation operation ID (the effect identity): the
sandbox is created with a unique name and its tags in one call, so a lost create
response, a retry or a lagging tag index rediscovers it instead of creating a twin
(Modal refuses a second running sandbox with the same name). Lookup failures are
never treated as absence.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from control.domain.digests import sha256_hex
from control.domain.errors import DomainError

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_NAME = "sbx-executor"
RUNTIME_PORT = 8790
DEFAULT_OPENCODE_VERSION = "1.18.34"
RESOURCE_CLASSES = {"standard": (2.0, 4096), "small": (1.0, 2048), "large": (4.0, 8192)}


def _sdk() -> Any:
    import modal  # only imported when a Modal executor is actually used

    return modal


class ModalExecutor:
    kind = "modal"

    def __init__(
        self,
        *,
        sdk: Any = None,
        opencode_version: str = DEFAULT_OPENCODE_VERSION,
        sandbox_timeout: int = 3600 * 6,
    ) -> None:
        self._sdk = sdk
        self.opencode_version = opencode_version
        self.sandbox_timeout = sandbox_timeout
        self._images: dict[tuple[str, str], Any] = {}

    @property
    def sdk(self) -> Any:
        return self._sdk or _sdk()

    def recipe_digest(self) -> str:
        parts = [self.opencode_version, str(RUNTIME_PORT)]
        for pkg in ("runtime/daemon", "runtime/harnesses", "runtime/security", "protocol"):
            for path in sorted((REPO_ROOT / pkg).rglob("*.py")):
                parts.append(sha256_hex(path.read_bytes()))
        return sha256_hex("|".join(parts))[:24]

    def _client(self, compute: dict[str, Any] | None) -> Any:
        if not compute or not compute.get("token_id") or not compute.get("token_secret"):
            raise DomainError(
                "connection_required", "Modal token is required for the Modal executor"
            )
        try:
            return self.sdk.Client.from_credentials(compute["token_id"], compute["token_secret"])
        except Exception as exc:  # SDK raises various auth errors
            raise DomainError(
                "credential_invalid", f"Modal rejected the credential ({type(exc).__name__})"
            ) from None

    def _app(self, client: Any) -> Any:
        return self.sdk.App.lookup(APP_NAME, create_if_missing=True, client=client)

    def image(self, client: Any, app: Any, connection_key: str) -> Any:
        key = (connection_key, self.recipe_digest())
        if key in self._images:
            return self._images[key]
        modal = self.sdk
        image = (
            modal.Image.debian_slim(python_version="3.12")
            .apt_install("git", "curl", "ca-certificates", "ripgrep", "procps")
            .run_commands(
                "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -",
                "apt-get install -y nodejs",
                f"npm install -g opencode-ai@{self.opencode_version}",
                "opencode --version",
                "git config --system user.name sbx && git config --system user.email sbx@localhost",
            )
            .add_local_python_source("runtime", "protocol", copy=True)
        )
        built = image.build(app)
        self._images[key] = built
        return built

    def capabilities(self) -> dict[str, Any]:
        return {
            "backend": "modal",
            "snapshot": "runtime_checkpoint",
            "native_pause": False,
            "multi_tenant": True,
        }

    @staticmethod
    def sandbox_name(operation_id: str) -> str:
        return f"sbx-{operation_id}"[:63]

    def _find(self, client: Any, app: Any, operation_id: str) -> dict[str, Any] | None:
        """Running allocation for the operation, else a finished one, else ``None``."""
        finished = None
        for sandbox in self.sdk.Sandbox.list(
            app_id=app.app_id, tags={"sbx_alloc": operation_id}, client=client
        ):
            handle = {"sandbox_id": sandbox.object_id, "operation_id": operation_id}
            if sandbox.poll() is None:
                return {**handle, "status": "running"}
            finished = finished or {**handle, "status": "terminated"}
        # The tag index can lag creation; the unique name resolves running sandboxes.
        try:
            named = self.sdk.Sandbox.from_name(
                APP_NAME, self.sandbox_name(operation_id), client=client
            )
        except self.sdk.exception.NotFoundError:
            return finished
        if named.poll() is None:
            return {
                "sandbox_id": named.object_id,
                "operation_id": operation_id,
                "status": "running",
            }
        return finished or {
            "sandbox_id": named.object_id,
            "operation_id": operation_id,
            "status": "terminated",
        }

    def allocate(self, spec: dict[str, Any], operation_id: str) -> dict[str, Any]:
        compute = spec.get("compute")
        client = self._client(compute)
        app = self._app(client)
        existing = self._lookup(client, app, operation_id)
        if existing is not None:
            return {**existing, "lease_id": spec["lease_id"]}
        image = self.image(client, app, spec.get("compute_connection_id") or "default")
        cpu, memory = RESOURCE_CLASSES.get(
            spec.get("resource_class") or "standard", RESOURCE_CLASSES["standard"]
        )
        tags = {
            "sbx_alloc": operation_id,
            "sbx_lease": spec["lease_id"],
            "sbx_session": spec["session_id"],
            "sbx_workspace": spec["workspace_id"],
        }
        try:
            sandbox = self.sdk.Sandbox.create(
                "python",
                "-m",
                "runtime.daemon.main",
                "--state-dir",
                "/sbx/state",
                "--work-dir",
                "/work",
                "--host",
                "0.0.0.0",
                "--port",
                str(RUNTIME_PORT),
                app=app,
                name=self.sandbox_name(operation_id),
                tags=tags,
                image=image,
                client=client,
                env={
                    "SBX_RUNTIME_KEY": spec["enrollment_key"],
                    "SBX_LEASE_ID": spec["lease_id"],
                    "SBX_LEASE_GENERATION": str(spec["generation"]),
                    "SBX_IMAGE_DIGEST": f"modal:{image.object_id}",
                    "PYTHONPATH": "/root",
                },
                secrets=[],
                encrypted_ports=[RUNTIME_PORT],
                timeout=self.sandbox_timeout,
                cpu=cpu,
                memory=memory,
            )
        except Exception as exc:
            # Lost response, a same-name twin already running, or a real failure: the
            # outcome is resolved by identity, never by creating again here.
            try:
                found = self._find(client, app, operation_id)
            except Exception:
                found = None
            if found is not None:
                return {**found, "lease_id": spec["lease_id"], "image_id": image.object_id}
            raise DomainError(
                "executor_unavailable",
                f"Modal sandbox create failed ({type(exc).__name__})",
                retryable=True,
            ) from None
        return {
            "sandbox_id": sandbox.object_id,
            "operation_id": operation_id,
            "lease_id": spec["lease_id"],
            "image_id": image.object_id,
            "status": "running",
        }

    def _lookup(self, client: Any, app: Any, operation_id: str) -> dict[str, Any] | None:
        try:
            return self._find(client, app, operation_id)
        except Exception as exc:
            raise DomainError(
                "executor_unavailable",
                f"Modal allocation lookup failed ({type(exc).__name__})",
                retryable=True,
            ) from None

    def lookup(self, operation_id: str, compute: dict[str, Any] | None) -> dict[str, Any] | None:
        client = self._client(compute)
        try:
            app = self._app(client)
        except Exception as exc:
            raise DomainError(
                "executor_unavailable",
                f"Modal app lookup failed ({type(exc).__name__})",
                retryable=True,
            ) from None
        return self._lookup(client, app, operation_id)

    def _sandbox(self, handle: dict[str, Any], compute: dict[str, Any] | None) -> Any:
        client = self._client(compute)
        return self.sdk.Sandbox.from_id(handle["sandbox_id"], client=client)

    def describe(self, handle: dict[str, Any], compute: dict[str, Any] | None) -> dict[str, Any]:
        try:
            sandbox = self._sandbox(handle, compute)
            return {"status": "running" if sandbox.poll() is None else "terminated"}
        except self.sdk.exception.NotFoundError:
            return {"status": "terminated"}

    def connect_runtime(self, handle: dict[str, Any], compute: dict[str, Any] | None) -> str:
        sandbox = self._sandbox(handle, compute)
        deadline = time.monotonic() + 180
        url = None
        while time.monotonic() < deadline:
            try:
                url = url or sandbox.tunnels(timeout=30)[RUNTIME_PORT].url
                if httpx.get(url + "/healthz", timeout=10).status_code == 200:
                    return url
            except Exception:
                pass
            if sandbox.poll() is not None:
                break
            time.sleep(1.0)
        raise DomainError(
            "executor_unavailable", "Modal runtime did not become reachable", retryable=True
        )

    def capture_filesystem(
        self, handle: dict[str, Any], prepared_manifest: dict[str, Any]
    ) -> dict[str, Any]:
        raise DomainError(
            "unsupported_capability",
            "backend-native capture not enabled; runtime checkpoints are used",
        )

    def restore(self, spec: dict[str, Any], snapshot_ref: str, operation_id: str) -> dict[str, Any]:
        raise DomainError(
            "unsupported_capability",
            "backend-native restore not enabled; runtime checkpoints are used",
        )

    def terminate(
        self, handle: dict[str, Any], operation_id: str, compute: dict[str, Any] | None
    ) -> bool:
        try:
            sandbox = self._sandbox(handle, compute)
            sandbox.terminate()
            for _ in range(30):
                if sandbox.poll() is not None:
                    return True
                time.sleep(1.0)
            return False
        except self.sdk.exception.NotFoundError:
            return True
