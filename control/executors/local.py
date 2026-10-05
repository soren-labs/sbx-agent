"""Isolated local process executor. Development only, never tenant isolation."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from control.domain.errors import DomainError
from control.runtime_client.client import RuntimeClient


class LocalExecutor:
    def __init__(self, root: Path, runtime_token=None):
        self.runtime_token = runtime_token
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def capabilities(self):
        return {"backend": "local", "production": False, "filesystem_checkpoint": True}

    def lookup(self, operation_id):
        path = self.root / (operation_id + ".json")
        return str(path) if path.exists() else None

    def allocate(self, spec, operation_id):
        previous = self.lookup(operation_id)
        if previous:
            return previous
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.root / spec.lease_id / "boot"),
            "LANG": "C.UTF-8",
            "SBX_RUNTIME_ROOT": str(self.root / spec.lease_id),
            "SBX_SESSION_ID": spec.session_id,
            "SBX_LEASE_ID": spec.lease_id,
            "SBX_LEASE_GENERATION": str(spec.generation),
            "SBX_RUNTIME_TOKEN": spec.runtime_token,
        }
        # Only daemon boot; provider work exclusively uses runtime frames.
        code = (
            "import os,uvicorn; from runtime.daemon.app import Runtime,create_runtime_app; "
            'r=Runtime(os.environ["SBX_RUNTIME_ROOT"],os.environ["SBX_SESSION_ID"],'
            'os.environ["SBX_LEASE_ID"],int(os.environ["SBX_LEASE_GENERATION"]),'
            f'os.environ["SBX_RUNTIME_TOKEN"]); '
            f'uvicorn.run(create_runtime_app(r),host="127.0.0.1",port={port},access_log=False)'
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        path = self.root / (operation_id + ".json")
        # Scoped runtime token is not stored; caller derives it from lease identity.
        path.write_text(json.dumps({"pid": process.pid, "port": port, "lease_id": spec.lease_id}))
        path.chmod(0o600)
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return str(path)
            except OSError:
                time.sleep(0.05)
        raise DomainError("executor_unavailable")

    def describe(self, handle):
        data = json.loads(Path(handle).read_text())
        proc = Path("/proc") / str(data["pid"])
        try:
            running = proc.exists() and proc.joinpath("stat").read_text().split()[2] != "Z"
            if running:
                inputs = proc.joinpath("environ").read_bytes().split(b"\x00")
                running = ("SBX_LEASE_ID=" + data["lease_id"]).encode() in inputs
        except OSError:
            running = False
        return {**data, "status": "ready" if running else "stopped"}

    def connect_runtime(self, handle, token=None):
        data = self.describe(handle)
        return RuntimeClient(f"http://127.0.0.1:{data['port']}", token or self.runtime_token)

    def capture_filesystem(self, handle, prepared_manifest):
        return prepared_manifest

    def restore(self, spec, snapshot_ref, operation_id):
        return self.allocate(spec, operation_id)

    def terminate(self, handle, operation_id):
        import signal

        data = self.describe(handle)
        if data["status"] == "stopped":
            return
        lease_root = (self.root / data["lease_id"]).resolve()
        # Development teardown is scoped to this lease's unique filesystem.
        # Provider processes have their own groups, so stopping daemon alone is insufficient.
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                if proc.joinpath("cwd").resolve().is_relative_to(lease_root):
                    pid = int(proc.name)
                    if os.getpgid(pid) == pid:
                        os.killpg(pid, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        try:
            os.killpg(data["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
