"""Local development/test Executor: one sbx-runtime daemon subprocess per lease.

Same runtime protocol as Modal; explicitly not a multi-tenant security sandbox.
Allocation discovery uses an operation-tag registry so a lost allocate
response is adopted instead of starting a second runtime.
"""

from __future__ import annotations

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

    def __init__(self, base_dir: Path, *, extra_env: dict[str, str] | None = None) -> None:
        self.base = Path(base_dir)
        self.registry = self.base / "registry"
        self.registry.mkdir(parents=True, exist_ok=True)
        self.extra_env = extra_env or {}
        self.digest = local_image_digest()

    def capabilities(self) -> dict[str, Any]:
        return {
            "backend": "local",
            "snapshot": "runtime_checkpoint",
            "native_pause": False,
            "multi_tenant": False,
        }

    def allocate(self, spec: dict[str, Any], operation_id: str) -> dict[str, Any]:
        lease_dir = self.base / "leases" / spec["lease_id"]
        (lease_dir / "home").mkdir(parents=True, exist_ok=True)
        port_file = lease_dir / "port"
        port_file.unlink(missing_ok=True)
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
        log = open(lease_dir / "daemon.log", "ab")  # noqa: SIM115 - handed to the child
        proc = subprocess.Popen(
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
        deadline = time.monotonic() + 20
        while not port_file.exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                raise DomainError(
                    "executor_unavailable", "local runtime failed to start", retryable=True
                )
            time.sleep(0.05)
        handle = {
            "pid": proc.pid,
            "port": int(port_file.read_text()),
            "dir": str(lease_dir),
            "lease_id": spec["lease_id"],
            "operation_id": operation_id,
        }
        (self.registry / f"{operation_id}.json").write_text(json.dumps(handle))
        return handle

    def lookup(self, operation_id: str, compute: dict[str, Any] | None) -> dict[str, Any] | None:
        path = self.registry / f"{operation_id}.json"
        if not path.exists():
            return None
        handle = json.loads(path.read_text())
        return handle if _alive(handle["pid"]) else None

    def describe(self, handle: dict[str, Any], compute: dict[str, Any] | None) -> dict[str, Any]:
        pid = handle.get("pid")
        return {"status": "running" if pid and _alive(pid) else "terminated"}

    def connect_runtime(self, handle: dict[str, Any], compute: dict[str, Any] | None) -> str:
        url = f"http://127.0.0.1:{handle['port']}"
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
