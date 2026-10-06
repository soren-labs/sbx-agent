"""Local development/test Executor: one sbx-runtime daemon subprocess per lease.

Same runtime protocol as Modal; explicitly not a multi-tenant security sandbox.
Allocation is idempotent by operation ID: under a per-operation file lock the
spawned daemon is registered *before* the readiness wait, a failed start is killed
(and only forgotten once its death is confirmed), and a retry or a lost allocate
response adopts the registered daemon instead of starting a second one.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from control.domain.digests import sha256_hex
from control.domain.errors import DomainError

REPO_ROOT = Path(__file__).resolve().parents[2]


def local_image_digest() -> str:
    digest = []
    for pkg in ("runtime/daemon", "runtime/harnesses", "runtime/security", "protocol"):
        for path in sorted((REPO_ROOT / pkg).rglob("*.py")):
            digest.append(sha256_hex(path.read_bytes()))
    return "local:" + sha256_hex("".join(digest))[:32]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split()[2] != "Z"
    except OSError:
        return True


class LocalExecutor:
    kind = "local"

    def __init__(
        self,
        base_dir: Path,
        *,
        extra_env: dict[str, str] | None = None,
        start_timeout: float = 20.0,
    ) -> None:
        self.base = Path(base_dir)
        self.registry = self.base / "registry"
        self.registry.mkdir(parents=True, exist_ok=True)
        self.extra_env = extra_env or {}
        self.digest = local_image_digest()
        self.start_timeout = start_timeout

    def capabilities(self) -> dict[str, Any]:
        return {
            "backend": "local",
            "snapshot": "runtime_checkpoint",
            "native_pause": False,
            "multi_tenant": False,
        }

    @contextlib.contextmanager
    def _operation_lock(self, operation_id: str) -> Any:
        with open(self.registry / f"{operation_id}.lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _spawn(self, spec: dict[str, Any], lease_dir: Path, port_file: Path) -> Any:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(lease_dir / "home"),
            "LANG": "C.UTF-8",
            "PYTHONPATH": str(REPO_ROOT),
            "SBX_RUNTIME_KEY": spec["enrollment_key"],
            "SBX_LEASE_ID": spec["lease_id"],
            "SBX_LEASE_GENERATION": str(spec["generation"]),
            "SBX_IMAGE_DIGEST": self.digest,
            **self.extra_env,
        }
        with open(lease_dir / "daemon.log", "ab") as log:
            return subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "runtime.daemon.main",
                    "--state-dir",
                    str(lease_dir / "state"),
                    "--work-dir",
                    str(lease_dir / "work"),
                    "--port",
                    "0",
                    "--port-file",
                    str(port_file),
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                cwd=str(REPO_ROOT),
            )

    def allocate(self, spec: dict[str, Any], operation_id: str) -> dict[str, Any]:
        with self._operation_lock(operation_id):
            existing = self.lookup(operation_id, None)
            if existing is not None:
                return existing
            lease_dir = self.base / "leases" / spec["lease_id"]
            (lease_dir / "home").mkdir(parents=True, exist_ok=True)
            port_file = lease_dir / "port"
            port_file.unlink(missing_ok=True)
            proc = self._spawn(spec, lease_dir, port_file)
            handle: dict[str, Any] = {
                "pid": proc.pid,
                "port": None,
                "dir": str(lease_dir),
                "lease_id": spec["lease_id"],
                "operation_id": operation_id,
            }
            record = self.registry / f"{operation_id}.json"
            # Registered before readiness: a crash or timeout below can never orphan it.
            record.write_text(json.dumps(handle))
            deadline = time.monotonic() + self.start_timeout
            while not port_file.exists():
                if proc.poll() is not None or time.monotonic() > deadline:
                    if self.terminate(handle, f"{operation_id}:failed-start", None):
                        record.unlink(missing_ok=True)
                    proc.poll()  # reap
                    raise DomainError(
                        "executor_unavailable", "local runtime failed to start", retryable=True
                    )
                time.sleep(0.05)
            handle["port"] = int(port_file.read_text())
            record.write_text(json.dumps(handle))
            return {**handle, "status": "running"}

    def lookup(self, operation_id: str, compute: dict[str, Any] | None) -> dict[str, Any] | None:
        path = self.registry / f"{operation_id}.json"
        if not path.exists():
            return None
        handle = json.loads(path.read_text())
        if not _alive(handle["pid"]):
            return {**handle, "status": "terminated"}
        if handle.get("port") is None:
            port_file = Path(handle["dir"]) / "port"
            if port_file.exists():
                handle["port"] = int(port_file.read_text())
        return {**handle, "status": "running"}

    def describe(self, handle: dict[str, Any], compute: dict[str, Any] | None) -> dict[str, Any]:
        pid = handle.get("pid")
        return {"status": "running" if pid and _alive(pid) else "terminated"}

    def connect_runtime(self, handle: dict[str, Any], compute: dict[str, Any] | None) -> str:
        port = handle.get("port")
        port_file = Path(handle.get("dir") or self.base) / "port"
        if port is None and port_file.exists():
            port = int(port_file.read_text())
        if port is None:
            raise DomainError(
                "executor_unavailable", "local runtime still starting", retryable=True
            )
        url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                    return url
            except httpx.HTTPError:
                time.sleep(0.05)
        raise DomainError("executor_unavailable", "local runtime not reachable", retryable=True)

    def capture_filesystem(
        self, handle: dict[str, Any], prepared_manifest: dict[str, Any]
    ) -> dict[str, Any]:
        raise DomainError(
            "unsupported_capability",
            "backend-native filesystem capture is not supported; use runtime checkpoints",
        )

    def restore(self, spec: dict[str, Any], snapshot_ref: str, operation_id: str) -> dict[str, Any]:
        raise DomainError(
            "unsupported_capability",
            "backend-native restore is not supported; use runtime checkpoints",
        )

    def terminate(
        self, handle: dict[str, Any], operation_id: str, compute: dict[str, Any] | None
    ) -> bool:
        pid = handle.get("pid")
        if not pid or not _alive(pid):
            return True
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        for _ in range(100):
            if not _alive(pid):
                return True
            time.sleep(0.05)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        time.sleep(0.2)
        return not _alive(pid)
