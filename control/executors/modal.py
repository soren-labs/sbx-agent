"""Modal Executor backend using the owner's selected Modal Connection.

Every sandbox is a full Linux VM (``runtime="vm"``); there is no second runtime for
any inference mode. Modal credentials stay in this worker; the sandbox receives only
the per-lease enrollment key. The bounded boot command starts sbx-runtime; Turns/files
use runtime operations over the encrypted tunnel.

One shared image recipe (CA certificates, Python, Node, Git, the pinned official CLIs
and the runtime daemon) is built once per owner workspace and recipe digest: ``prewarm``
builds it when the Modal Connection is verified, so allocation only resolves the cache.

Allocation is idempotent by the allocation operation ID (the effect identity): the
sandbox is created with a unique name and its tags in one call, so a lost create
response, a retry or a lagging tag index rediscovers it instead of creating a twin
(Modal refuses a second running sandbox with the same name). Lookup failures are
never treated as absence.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from protocol.capabilities import HARNESS_CLI_PACKAGES

from control.domain.digests import sha256_hex
from control.domain.errors import DomainError

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_NAME = "sbx-executor"
RUNTIME_PORT = 8790
# The only Modal sandbox runtime SBX uses; requires modal>=1.6.1.
SANDBOX_RUNTIME = "vm"
RESOURCE_CLASSES = {"standard": (2.0, 4096), "small": (1.0, 2048), "large": (4.0, 8192)}
# A Setup VM only runs the official CLI's login; it never hosts a workload.
SETUP_RESOURCES = (1.0, 1024)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _sdk() -> Any:
    import modal  # only imported when a Modal executor is actually used

    return modal


class ModalExecutor:
    kind = "modal"

    def __init__(
        self,
        *,
        sdk: Any = None,
        cli_packages: dict[str, tuple[str, str]] | None = None,
        sandbox_timeout: int = 3600 * 6,
    ) -> None:
        self._sdk = sdk
        # Official CLI npm distributions baked into the executor image (pinned versions).
        self.cli_packages = dict(cli_packages or HARNESS_CLI_PACKAGES)
        self.sandbox_timeout = sandbox_timeout
        self._images: dict[tuple[str, str], Any] = {}
        self._image_lock = threading.Lock()

    @property
    def sdk(self) -> Any:
        return self._sdk or _sdk()

    def recipe_digest(self) -> str:
        parts = [f"{name}@{version}" for name, version in sorted(self.cli_packages.values())]
        parts.append(str(RUNTIME_PORT))
        for pkg in (
            "runtime/daemon",
            "runtime/harnesses",
            "runtime/security",
            "runtime/subscriptions",
            "protocol",
        ):
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

    def image_recipe(self) -> Any:
        """The single shared runtime image definition (unbuilt)."""
        modal = self.sdk
        return (
            modal.Image.debian_slim(python_version="3.12")
            .apt_install("git", "curl", "ca-certificates", "ripgrep", "procps")
            .run_commands(
                "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -",
                "apt-get install -y nodejs",
                "npm install -g "
                + " ".join(f"{name}@{version}" for name, version in self.cli_packages.values()),
                "opencode --version && codex --version && claude --version"
                " && grok --version && cmd --version",
                "git config --system user.name sbx && git config --system user.email sbx@localhost",
            )
            .add_local_python_source("runtime", "protocol", copy=True)
        )

    def image(self, client: Any, app: Any, connection_key: str) -> Any:
        """Built image for the owner workspace; a cache lookup once the recipe was built."""
        key = (connection_key, self.recipe_digest())
        with self._image_lock:
            if key in self._images:
                return self._images[key]
        built = self.image_recipe().build(app)
        with self._image_lock:
            self._images[key] = built
        return built

    def prewarm(self, compute: dict[str, Any] | None, connection_key: str) -> dict[str, Any]:
        """Build the shared image ahead of the first allocation (idempotent)."""
        client = self._client(compute)
        started = time.monotonic()
        try:
            image = self.image(client, self._app(client), connection_key)
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError(
                "executor_unavailable",
                f"Modal image build failed ({type(exc).__name__})",
                retryable=True,
            ) from None
        return {
            "image_id": image.object_id,
            "recipe_digest": self.recipe_digest(),
            "image_resolve_ms": _ms(started),
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "backend": "modal",
            "snapshot": "runtime_checkpoint",
            "native_pause": False,
            "multi_tenant": True,
            "runtime": SANDBOX_RUNTIME,
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
        started = time.monotonic()
        image = self.image(client, app, spec.get("compute_connection_id") or "default")
        timings = {"image_resolve_ms": _ms(started)}
        started = time.monotonic()
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
            sandbox = self._create(
                client,
                app,
                image,
                operation_id,
                tags,
                (
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
                ),
                env={
                    "SBX_RUNTIME_KEY": spec["enrollment_key"],
                    "SBX_LEASE_ID": spec["lease_id"],
                    "SBX_LEASE_GENERATION": str(spec["generation"]),
                    "SBX_IMAGE_DIGEST": f"modal:{image.object_id}",
                    "PYTHONPATH": "/root",
                },
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
            "runtime": SANDBOX_RUNTIME,
            "timings": {**timings, "sandbox_create_ms": _ms(started)},
            "status": "running",
        }

    def _create(
        self,
        client: Any,
        app: Any,
        image: Any,
        operation_id: str,
        tags: dict[str, str],
        argv: tuple[str, ...],
        **options: Any,
    ) -> Any:
        """The only place SBX creates a Modal sandbox: always a VM, never with ambient identity."""
        return self.sdk.Sandbox.create(
            *argv,
            app=app,
            name=self.sandbox_name(operation_id),
            tags=tags,
            image=image,
            runtime=SANDBOX_RUNTIME,
            client=client,
            secrets=[],
            include_oidc_identity_token=False,
            **options,
        )

    # ------------------------------------------------------------ subscription setup
    def setup_start(self, spec: dict[str, Any], operation_id: str) -> dict[str, Any]:
        """Start (or adopt) the Setup VM for one login attempt on a private Slot Volume.

        Idempotent by ``operation_id`` like ``allocate``. The Volume lives in the owner's
        Modal workspace and is mounted only here and, later, into the Slot's single Worker.
        """
        compute = spec.get("compute")
        client = self._client(compute)
        app = self._app(client)
        existing = self._lookup(client, app, operation_id)
        if existing is not None:
            return existing
        try:
            image = self.image(client, app, spec.get("compute_connection_id") or "default")
            volume = self.sdk.Volume.from_name(
                spec["volume_name"], create_if_missing=True, version=2, client=client
            )
            sandbox = self._create(
                client,
                app,
                image,
                operation_id,
                {"sbx_alloc": operation_id, **spec["tags"]},
                ("python", "-m", "runtime.subscriptions.setup"),
                env={
                    **spec["env"],
                    "SBX_SETUP_SPEC": json.dumps(spec["setup"]),
                    "PYTHONPATH": "/root",
                },
                volumes={spec["mount"]: volume},
                timeout=int(spec["timeout"]),
                cpu=SETUP_RESOURCES[0],
                memory=SETUP_RESOURCES[1],
            )
        except Exception as exc:
            try:
                found = self._find(client, app, operation_id)
            except Exception:
                found = None
            if found is not None:
                return found
            raise DomainError(
                "executor_unavailable",
                f"Modal setup VM create failed ({type(exc).__name__})",
                retryable=True,
            ) from None
        return {"sandbox_id": sandbox.object_id, "operation_id": operation_id, "status": "running"}

    def setup_observe(
        self, handle: dict[str, Any], compute: dict[str, Any] | None, state_path: str
    ) -> dict[str, Any]:
        """VM liveness plus the supervisor's state file (never profile contents)."""
        try:
            sandbox = self._sandbox(handle, compute)
            if sandbox.poll() is not None:
                return {"status": "terminated", "state": None}
            process = sandbox.exec("cat", state_path, timeout=20)
            output = process.stdout.read()
            process.wait()
        except self.sdk.exception.NotFoundError:
            return {"status": "terminated", "state": None}
        except Exception as exc:
            raise DomainError(
                "executor_unavailable",
                f"Modal setup VM is unreachable ({type(exc).__name__})",
                retryable=True,
            ) from None
        try:
            state = json.loads(output) if process.returncode == 0 else None
        except ValueError:
            state = None
        return {"status": "running", "state": state if isinstance(state, dict) else None}

    def volume_delete(self, compute: dict[str, Any] | None, volume_name: str) -> bool:
        """Delete a Slot's private Volume (and with it the login); missing is success."""
        client = self._client(compute)
        try:
            self.sdk.Volume.objects.delete(volume_name, allow_missing=True, client=client)
        except Exception as exc:
            raise DomainError(
                "executor_unavailable",
                f"Modal volume delete failed ({type(exc).__name__})",
                retryable=True,
            ) from None
        return True

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
