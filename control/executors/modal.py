"""Modal executor uses only explicitly supplied user credentials.

Provider execution is never sandbox.exec: the sole boot command starts daemon.
Allocation names/tags are stable effect identities, not business authority.
"""

from pathlib import Path

from control.domain.errors import DomainError
from control.runtime_client.client import RuntimeClient


class ModalExecutor:
    def __init__(self, credentials, runtime_token, *, sdk=None):
        if sdk is None:
            import modal

            sdk = modal
        self.sdk = sdk
        self.client = sdk.Client.from_credentials(
            credentials["token_id"], credentials["token_secret"]
        )
        self.runtime_token = runtime_token
        self.app = sdk.App.lookup(
            "sbx-unified-benchmark", create_if_missing=True, client=self.client
        )

    def capabilities(self):
        return {
            "backend": "modal",
            "production": True,
            "filesystem_checkpoint": True,
            "memory_restore": False,
            "protocol_major": 1,
        }

    def lookup(self, operation_id):
        for sandbox in self.sdk.Sandbox.list(
            app_id=self.app.app_id, tags={"sbx_effect": operation_id}, client=self.client
        ):
            return sandbox.object_id
        return None

    def image(self):
        root = Path(__file__).resolve().parents[2]
        return (
            self.sdk.Image.debian_slim(python_version="3.12")
            .apt_install("git", "curl", "ca-certificates", "nodejs", "npm")
            .run_commands("npm install -g opencode-ai@1.18.29")
            .pip_install(
                "fastapi==0.135.1", "uvicorn==0.41.0", "httpx==0.28.1", "websockets==15.0.1"
            )
            .add_local_dir(root / "protocol", "/opt/sbx/protocol", copy=True)
            .add_local_dir(root / "runtime/daemon", "/opt/sbx/runtime/daemon", copy=True)
            .add_local_dir(root / "runtime/harnesses", "/opt/sbx/runtime/harnesses", copy=True)
            .add_local_dir(root / "runtime/security", "/opt/sbx/runtime/security", copy=True)
        )

    def allocate(self, spec, operation_id):
        existing = self.lookup(operation_id)
        if existing:
            return existing
        try:
            sandbox = self.sdk.Sandbox.create(
                "python",
                "-m",
                "runtime.daemon.main",
                name="sbx-" + operation_id,
                app=self.app,
                image=self.image(),
                client=self.client,
                tags={
                    "sbx_effect": operation_id,
                    "sbx_lease": spec.lease_id,
                    "sbx_session": spec.session_id,
                    "sbx_workspace": spec.workspace_id,
                },
                env={
                    "PYTHONPATH": "/opt/sbx",
                    "SBX_RUNTIME_ROOT": "/work",
                    "SBX_SESSION_ID": spec.session_id,
                    "SBX_LEASE_ID": spec.lease_id,
                    "SBX_LEASE_GENERATION": str(spec.generation),
                    "SBX_RUNTIME_TOKEN": self.runtime_token,
                    "SBX_IMAGE_DIGEST": spec.image_digest,
                },
                encrypted_ports=[8792],
                timeout=1800,
                cpu=2,
                memory=4096,
                secrets=[],
            )
            return sandbox.object_id
        except Exception:
            found = self.lookup(operation_id)
            if found:
                return found
            raise DomainError("executor_unavailable") from None

    def describe(self, handle):
        sandbox = self.sdk.Sandbox.from_id(handle, client=self.client)
        return {"status": "ready" if sandbox.poll() is None else "stopped"}

    def connect_runtime(self, handle):
        sandbox = self.sdk.Sandbox.from_id(handle, client=self.client)
        tunnel = sandbox.tunnels(timeout=60)[8792]
        return RuntimeClient(tunnel.url, self.runtime_token)

    def capture_filesystem(self, handle, prepared_manifest):
        return prepared_manifest

    def restore(self, spec, snapshot_ref, operation_id):
        return self.allocate(spec, operation_id)

    def terminate(self, handle, operation_id):
        try:
            self.sdk.Sandbox.from_id(handle, client=self.client).terminate()
        except self.sdk.exception.NotFoundError:
            pass
