"""Local executor backend (RFC 167 §03).

Development/test implementation of the same protocol — spawns
``python -m runtime.daemon.main`` as a subprocess whose daemon connects to
the loopback ingress. NOT a hosted multi-tenant security sandbox.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

from .port import (
    AllocationSpec,
    ExecutorCapabilities,
    ExecutorError,
    ExecutorHandle,
)

_ALLOCATION_MARKER = "ALLOCATION.json"


class LocalExecutorBackend:
    backend = "local"

    def __init__(
        self, run_root: Path, ingress_endpoint: str, repo_root: Path | None = None
    ) -> None:
        self._run_root = Path(run_root)
        self._ingress_endpoint = ingress_endpoint
        self._repo_root = Path(repo_root) if repo_root else Path.cwd()

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            runtime_transport="tcp",
            filesystem_snapshot=True,
            pause_restore=False,
            restore=True,
        )

    # -- port ---------------------------------------------------------

    def allocate(self, spec: AllocationSpec, operation_id: str) -> ExecutorHandle:
        existing = self.lookup(operation_id)
        if existing is not None:
            return existing  # adopt: never double-allocate on a lost response
        root = self._run_root / spec.lease_id
        worktree = root / "worktree"
        state = root / "state"
        worktree.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        (root / _ALLOCATION_MARKER).write_text(
            json.dumps(
                {
                    "operation_id": operation_id,
                    "lease_id": spec.lease_id,
                    "session_id": spec.session_id,
                    "created_at": time.time(),
                }
            )
        )
        argv = [
            sys.executable,
            "-m",
            "runtime.daemon.main",
            "--lease-id",
            spec.lease_id,
            "--lease-generation",
            str(spec.lease_generation),
            "--connect",
            spec.enrollment_ref or self._ingress_endpoint,
            "--worktree",
            str(worktree),
            "--state",
            str(state),
        ]
        if spec.image_digest:
            argv += ["--image-digest", spec.image_digest]
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TERM") if k in os.environ}
        env["PYTHONPATH"] = str(self._repo_root) + os.pathsep + os.environ.get("PYTHONPATH", "")
        env["SBX_ENROLLMENT_TOKEN"] = spec.env.get("SBX_ENROLLMENT_TOKEN", "")
        # Harness/provider passthrough explicitly named on the spec (e.g.
        # OPENCODE_BIN for deterministic fakes in tests).
        for key in ("OPENCODE_BIN", "CODEX_BIN"):
            if key in os.environ:
                env[key] = os.environ[key]
            elif key in spec.env:
                env[key] = spec.env[key]
        for key, value in spec.env.items():
            env.setdefault(key, value)
        log = (state / "daemon.log").open("ab")
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise ExecutorError("allocate_failed", str(exc), retryable=True) from exc
        handle = ExecutorHandle(
            backend=self.backend,
            handle_id=uuid.uuid4().hex,
            metadata={
                "pid": proc.pid,
                "root": str(root),
                "worktree": str(worktree),
                "state": str(state),
                "endpoint": spec.enrollment_ref or self._ingress_endpoint,
                "allocation_operation_id": operation_id,
            },
        )
        return handle

    def lookup(self, operation_id: str) -> ExecutorHandle | None:
        if not self._run_root.is_dir():
            return None
        for child in sorted(self._run_root.iterdir()):
            marker = child / _ALLOCATION_MARKER
            if not marker.is_file():
                continue
            try:
                record = json.loads(marker.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if record.get("operation_id") != operation_id:
                continue
            lease_id = str(record.get("lease_id") or child.name)
            pid = _read_pid(child)
            return ExecutorHandle(
                backend=self.backend,
                handle_id=lease_id,
                metadata={
                    "pid": pid,
                    "root": str(child),
                    "worktree": str(child / "worktree"),
                    "state": str(child / "state"),
                    "endpoint": self._ingress_endpoint,
                    "allocation_operation_id": operation_id,
                    "adopted": pid is None,
                },
            )
        return None

    def describe(self, handle: ExecutorHandle) -> dict:
        pid = handle.metadata.get("pid")
        alive = pid is not None and _pid_alive(int(pid))
        return {
            "alive": alive,
            "pid": pid,
            "worktree": handle.metadata.get("worktree"),
            "state": handle.metadata.get("state"),
            "endpoint": handle.metadata.get("endpoint"),
        }

    def connect_runtime(self, handle: ExecutorHandle) -> dict:
        return {"endpoint": handle.metadata.get("endpoint"), "transport": "tcp"}

    def capture_filesystem(self, handle: ExecutorHandle, prepared_manifest: dict) -> dict:
        worktree = Path(handle.metadata["worktree"])
        state = Path(handle.metadata["state"])
        captures = state / "captures"
        captures.mkdir(parents=True, exist_ok=True)
        import hashlib

        blob = json.dumps(prepared_manifest, sort_keys=True).encode()
        digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        tar_path = captures / (digest.split(":", 1)[1] + ".tar")
        with tarfile.open(tar_path, "w") as tar:
            for child in sorted(worktree.iterdir()):
                tar.add(child, arcname=child.name)
        return {
            "kind": "local_tar",
            "path": str(tar_path),
            "manifest_digest": digest,
        }

    def restore(
        self, spec: AllocationSpec, snapshot_ref: dict, operation_id: str
    ) -> ExecutorHandle:
        if snapshot_ref.get("kind") != "local_tar":
            raise ExecutorError("snapshot_unsupported", "local backend needs local_tar ref")
        handle = self.allocate(spec, operation_id)
        worktree = Path(handle.metadata["worktree"])
        with tarfile.open(snapshot_ref["path"]) as tar:
            tar.extractall(worktree, filter="data")
        return handle

    def terminate(self, handle: ExecutorHandle, operation_id: str) -> None:
        pid = handle.metadata.get("pid")
        if pid is not None:
            try:
                os.killpg(int(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            deadline = time.time() + 5
            while time.time() < deadline and _pid_alive(int(pid)):
                time.sleep(0.05)
            if _pid_alive(int(pid)):
                try:
                    os.killpg(int(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        root = handle.metadata.get("root")
        if root:
            shutil.rmtree(root, ignore_errors=True)


def _read_pid(root: Path) -> int | None:
    proc = subprocess.run(
        ["pgrep", "-f", f"runtime.daemon.main.*--lease-id {root.name}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return int(proc.stdout.strip().splitlines()[0])
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
