"""Resident executor: an operator's own Linux workstation or VPS reached over OpenSSH.

The control plane runs exactly one fixed program on the machine, the official
``python -m runtime.resident.agent <verb>`` from the operator-configured checkout, with
operator-fixed SSH parameters (explicit host, port, user, key and known_hosts file;
``-F none``, ``BatchMode``, ``StrictHostKeyChecking=yes``). No API caller can choose any of
them, and there is no generic remote command: the agent's verbs are ``probe``,
``allocate``, ``lookup`` and ``terminate``.

The agent starts the same sbx-runtime daemon as Local/Modal, detached from the SSH
session, in a per-lease directory (worktree, HOME, state) with an allow-listed
environment. Runtime frames travel over an SSH local forward that is re-created on
demand (``resident://`` endpoints resolve through :meth:`ResidentExecutor.endpoint`), so a
dropped link or a restarted control plane reconnects to the same daemon and epoch.

SSH failures are never read as absence or termination: they raise retryable
``executor_unavailable``. Allocation is idempotent by operation ID on the machine itself.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from control.application.ports import RuntimeUnavailable
from control.domain.errors import DomainError

REPO_ROOT = Path(__file__).resolve().parents[2]
# Must match runtime/resident/agent.py DIGEST_PACKAGES (parity is tested).
DIGEST_PACKAGES = (
    "protocol",
    "runtime/daemon",
    "runtime/harnesses",
    "runtime/resident",
    "runtime/security",
    "runtime/subscriptions",
)
BIN_OVERRIDES = frozenset(
    {"OPENCODE_BIN", "CODEX_BIN", "CLAUDE_BIN", "GROK_BIN", "COMMANDCODE_BIN", "AGY_BIN"}
)
SCHEME = "resident://"
# A resident runtime keeps running while unreachable (its lease authority lasts 1800 s), so
# reconcile waits this long for the machine to come back before declaring outcome unknown.
UNREACHABLE_GRACE_SECONDS = 900.0
REACHABILITY = (
    "reachable",
    "unreachable",
    "host_key_unverified",
    "auth_failed",
    "runtime_missing",
    "runtime_mismatch",
    "unchecked",
)
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$")
_USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")


def runtime_source_digest(root: Path = REPO_ROOT) -> str:
    parts = []
    for package in DIGEST_PACKAGES:
        for path in sorted((root / package).rglob("*.py")):
            parts.append(hashlib.sha256(path.read_bytes()).hexdigest())
    return hashlib.sha256("".join(parts).encode()).hexdigest()[:32]


# --------------------------------------------------------------------------- the target
@dataclass(frozen=True)
class Target:
    """Operator-fixed SSH parameters of one resident machine (server-side only)."""

    host: str
    user: str
    identity_file: str
    known_hosts_file: str
    remote_python: str
    remote_root: str
    port: int = 22
    state_root: str | None = None
    remote_path: str | None = None
    cli_overrides: dict[str, str] = field(default_factory=dict)
    connect_timeout: int = 10

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Target:
        target = cls(
            host=str(data["host"]),
            user=str(data["user"]),
            identity_file=str(data["identity_file"]),
            known_hosts_file=str(data["known_hosts_file"]),
            remote_python=str(data["remote_python"]),
            remote_root=str(data["remote_root"]),
            port=int(data["port"]) if data.get("port") is not None else 22,
            state_root=data.get("state_root") or None,
            remote_path=data.get("remote_path") or None,
            cli_overrides=dict(data.get("cli_overrides") or {}),
            connect_timeout=int(data.get("connect_timeout") or 10),
        )
        target.validate()
        return target

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "identity_file": self.identity_file,
            "known_hosts_file": self.known_hosts_file,
            "remote_python": self.remote_python,
            "remote_root": self.remote_root,
            "state_root": self.state_root,
            "remote_path": self.remote_path,
            "cli_overrides": dict(self.cli_overrides),
            "connect_timeout": self.connect_timeout,
        }

    def validate(self) -> None:
        def bad(name: str, why: str) -> DomainError:
            return DomainError("validation_failed", f"{name} {why}", details={"field": name})

        if not _HOST.match(self.host):
            raise bad("host", "must be a host name or address")
        if not _USER.match(self.user):
            raise bad("user", "must be a Unix user name")
        if not 0 < self.port < 65536:
            raise bad("port", "must be a TCP port")
        for name in ("identity_file", "known_hosts_file", "remote_python", "remote_root"):
            value = getattr(self, name)
            if not value.startswith("/") or "\n" in value:
                raise bad(name, "must be an absolute path")
        if self.state_root and not self.state_root.startswith(("/", "~/")):
            raise bad("state_root", "must be an absolute path or start with ~/")
        if self.remote_path and not all(d.startswith("/") for d in self.remote_path.split(":")):
            raise bad("remote_path", "must list absolute directories")
        for key, value in self.cli_overrides.items():
            if key not in BIN_OVERRIDES or not str(value).startswith("/"):
                raise bad("cli_overrides", f"{key} is not an allowed absolute CLI override")
        if not 1 <= self.connect_timeout <= 60:
            raise bad("connect_timeout", "must be 1..60 seconds")

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------- transport
class Unreachable(Exception):
    """SSH could not run the agent; ``status`` is one of REACHABILITY."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(f"{status}: {reason}")
        self.status = status
        self.reason = reason


class AgentRefused(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def ssh_argv(target: Target, ssh: str = "ssh", *, alive: tuple[int, int] = (15, 3)) -> list[str]:
    """Strict, explicit OpenSSH options. No ssh_config, alias, agent forwarding or TOFU."""
    return [
        ssh,
        "-F",
        "none",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={target.known_hosts_file}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        target.identity_file,
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "RequestTTY=no",
        "-o",
        f"ConnectTimeout={target.connect_timeout}",
        "-o",
        f"ServerAliveInterval={alive[0]}",
        "-o",
        f"ServerAliveCountMax={alive[1]}",
        "-o",
        "LogLevel=ERROR",
        "-p",
        str(target.port),
        "-l",
        target.user,
        target.host,
    ]


def remote_command(target: Target, verb: str) -> str:
    parts = [
        target.remote_python,
        "-E",
        "-s",
        "-m",
        "runtime.resident.agent",
    ]
    if target.state_root:
        parts += ["--state-root", target.state_root]
    parts.append(verb)
    return f"cd -- {shlex.quote(target.remote_root)} && exec " + " ".join(
        shlex.quote(p) if not p.startswith("~/") else "~/" + shlex.quote(p[2:]) for p in parts
    )


# ssh's own messages name the host and port; the API only ever shows these phrases.
_REASONS = (
    ("host key verification failed", "host key verification failed"),
    ("remote host identification has changed", "the host key changed"),
    ("host key is known", "host key verification failed"),
    ("permission denied", "the SSH key was not accepted"),
    ("too many authentication failures", "the SSH key was not accepted"),
    ("connection refused", "connection refused"),
    ("timed out", "connection timed out"),
    ("no route to host", "no route to host"),
    ("network is unreachable", "network unreachable"),
    ("could not resolve hostname", "host name did not resolve"),
    ("name or service not known", "host name did not resolve"),
    ("connection closed", "connection closed by the machine"),
    ("connection reset", "connection reset"),
    ("broken pipe", "connection lost"),
    ("no such file or directory", "remote python or checkout not found"),
    ("not found", "remote python or checkout not found"),
    ("no module named", "the remote checkout has no SBX runtime"),
)


def _reason(lowered: str, fallback: str) -> str:
    for needle, phrase in _REASONS:
        if needle in lowered:
            return phrase
    return fallback


def classify(returncode: int, stderr: str) -> tuple[str, str]:
    """Map an SSH/agent launch failure to (reachability status, fixed reason phrase)."""
    lowered = stderr.lower()
    if returncode == 255:
        if (
            "host key verification failed" in lowered
            or "remote host identification has changed" in lowered
            or "host key is known" in lowered
        ):
            return "host_key_unverified", _reason(lowered, "host key verification failed")
        if "permission denied" in lowered or "too many authentication failures" in lowered:
            return "auth_failed", _reason(lowered, "the SSH key was not accepted")
        return "unreachable", _reason(lowered, "ssh could not connect")
    return "runtime_missing", _reason(
        lowered, f"the remote agent did not answer (exit {returncode})"
    )


def ssh_env() -> dict[str, str]:
    """ssh gets no SBX secrets: only what OpenSSH itself needs."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C"}
    for key in ("HOME", "SSH_AUTH_SOCK", "USER", "LOGNAME"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


class SshTransport:
    def __init__(self, ssh: str = "ssh", timeout: float = 60.0) -> None:
        self.ssh = ssh
        self.timeout = timeout

    def call(
        self,
        target: Target,
        verb: str,
        request: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        argv = [*ssh_argv(target, self.ssh), "--", remote_command(target, verb)]
        try:
            proc = subprocess.run(
                argv,
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                env=ssh_env(),
            )
        except subprocess.TimeoutExpired:
            raise Unreachable("unreachable", "ssh call timed out") from None
        except OSError as exc:
            raise Unreachable("unreachable", f"cannot run ssh ({exc.strerror})") from None
        line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        try:
            reply = json.loads(line) if line else None
        except ValueError:
            reply = None
        if not isinstance(reply, dict):
            raise Unreachable(*classify(proc.returncode, proc.stderr))
        if not reply.get("ok"):
            error = reply.get("error") or {}
            raise AgentRefused(
                str(error.get("code") or "agent_error"),
                str(error.get("message") or ""),
                error.get("details") or {},
            )
        return reply


# ------------------------------------------------------------------------------ tunnels
def _halt(proc: subprocess.Popen[bytes]) -> None:
    """Stop a watchdog and its ssh: closing the pipe asks it; SIGTERM is the fallback."""
    if proc.stdin is not None and not proc.stdin.closed:
        try:
            proc.stdin.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()  # the watchdog forwards it to ssh
        proc.wait()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# Each forward runs under this watchdog, which holds a pipe from the control plane: when the
# control plane exits for any reason (SIGTERM re-raised by uvicorn, SIGKILL, crash) the pipe
# closes and the watchdog stops ssh, so no forward outlives the process that owns it.
_WATCHDOG = """
import signal, subprocess, sys, threading
child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
def stop(*_):
    if child.poll() is None:
        child.terminate()
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
def owner_gone():
    sys.stdin.buffer.read()
    stop()
threading.Thread(target=owner_gone, daemon=True).start()
sys.exit(child.wait())
"""


@dataclass
class _Tunnel:
    proc: subprocess.Popen[bytes]
    local_port: int
    remote_port: int
    fingerprint: str
    log: Any


class Tunnels:
    """One SSH local forward per lease, (re)created on demand and closed on terminate."""

    def __init__(self, ssh: str = "ssh") -> None:
        self.ssh = ssh
        self._live: dict[str, _Tunnel] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        atexit.register(self.close_all)

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def ensure(self, key: str, remote_port: int, target: Callable[[], Target]) -> int:
        with self._lock(key):
            tunnel = self._live.get(key)
            if (
                tunnel is not None
                and tunnel.proc.poll() is None
                and tunnel.remote_port == remote_port
            ):
                return tunnel.local_port
            self._stop(key)
            resolved = target()
            last = ("unreachable", "tunnel did not start")
            for _ in range(3):
                port = _free_port()
                log = tempfile.TemporaryFile()
                argv = [
                    *ssh_argv(resolved, self.ssh, alive=(5, 3)),
                    "-N",
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-L",
                    f"127.0.0.1:{port}:127.0.0.1:{int(remote_port)}",
                ]
                proc = subprocess.Popen(
                    [sys.executable, "-I", "-c", _WATCHDOG, *argv],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    env=ssh_env(),
                )
                deadline = time.monotonic() + resolved.connect_timeout + 5
                while time.monotonic() < deadline and proc.poll() is None:
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=1):
                            pass
                    except OSError:
                        time.sleep(0.1)
                        continue
                    if proc.poll() is None:
                        self._live[key] = _Tunnel(
                            proc, port, int(remote_port), resolved.fingerprint(), log
                        )
                        return port
                _halt(proc)
                log.seek(0)
                stderr = log.read().decode("utf-8", "replace")
                log.close()
                last = classify(255, stderr)
                if "address already in use" not in stderr.lower():
                    break
            raise Unreachable(*last)

    def _stop(self, key: str) -> None:
        tunnel = self._live.pop(key, None)
        if tunnel is None:
            return
        _halt(tunnel.proc)
        tunnel.log.close()

    def close(self, key: str) -> None:
        with self._lock(key):
            self._stop(key)

    def close_all(self) -> None:
        for key in list(self._live):
            self.close(key)

    def alive(self, key: str) -> bool:
        tunnel = self._live.get(key)
        return tunnel is not None and tunnel.proc.poll() is None


# ----------------------------------------------------------------------------- executor
def _unavailable(exc: Unreachable) -> DomainError:
    return DomainError(
        "executor_unavailable",
        f"resident machine is {exc.status.replace('_', ' ')}: {exc.reason}",
        details={"reachability": exc.status},
        retryable=True,
    )


def _refused(exc: AgentRefused) -> DomainError:
    if exc.code == "runtime_mismatch":
        return DomainError(
            "runtime_incompatible",
            "the SBX checkout on the resident machine differs from the control plane's; "
            "update it to the same version",
            details={k: v for k, v in exc.details.items() if k.endswith("digest")},
        )
    if exc.code == "unsupported_platform":
        return DomainError("unsupported_capability", exc.message)
    return DomainError(
        "executor_unavailable",
        f"resident agent refused ({exc.code}): {exc.message}"[:300],
        retryable=exc.code in ("busy", "start_failed", "agent_error"),
    )


def _target(compute: dict[str, Any] | None) -> Target:
    if not compute or not compute.get("target"):
        raise DomainError("executor_unavailable", "the resident machine of this lease is not known")
    return Target.from_dict(compute["target"])


class ResidentExecutor:
    kind = "resident"

    def __init__(
        self,
        *,
        ssh: str = "ssh",
        call_timeout: float = 60.0,
        expected_digest: str | None = None,
        probe_ttl: float = 5.0,
        unreachable_grace: float = UNREACHABLE_GRACE_SECONDS,
        transport: SshTransport | None = None,
        health_timeout: float = 10.0,
    ) -> None:
        self.transport = transport or SshTransport(ssh, call_timeout)
        self.tunnels = Tunnels(ssh)
        self.expected_digest = expected_digest or runtime_source_digest()
        self.probe_ttl = probe_ttl
        self.unreachable_grace = unreachable_grace
        self.health_timeout = health_timeout
        self._probes: dict[str, tuple[float, dict[str, Any]]] = {}

    def capabilities(self) -> dict[str, Any]:
        return {
            "backend": "resident",
            "snapshot": "runtime_checkpoint",
            "native_pause": False,
            "multi_tenant": False,
            "unreachable_grace_seconds": self.unreachable_grace,
        }

    # ------------------------------------------------------------------ discovery
    def probe(self, target_data: dict[str, Any], *, fresh: bool = False) -> dict[str, Any]:
        """Live, bounded, read-only reachability check (cached briefly in memory)."""
        target = Target.from_dict(target_data)
        key = target.fingerprint()
        cached = self._probes.get(key)
        if cached and not fresh and time.monotonic() - cached[0] < self.probe_ttl:
            return cached[1]
        checked = time.time()
        try:
            reply = self.transport.call(
                target,
                "probe",
                {"path": target.remote_path, "cli_overrides": target.cli_overrides},
                timeout=target.connect_timeout + 20,
            )
            match = reply.get("runtime_digest") == self.expected_digest
            result = {
                "status": "reachable" if match else "runtime_mismatch",
                "reason": None
                if match
                else "the SBX checkout on the machine differs from the control plane's",
                "runtime": {
                    "digest": reply.get("runtime_digest"),
                    "expected_digest": self.expected_digest,
                    "digest_match": match,
                    "python": reply.get("python"),
                    "harnesses": sorted(k for k, v in (reply.get("harnesses") or {}).items() if v),
                    "live_runtimes": reply.get("live_leases"),
                },
            }
        except Unreachable as exc:
            result = {"status": exc.status, "reason": exc.reason, "runtime": None}
        except AgentRefused as exc:
            result = {
                "status": "runtime_missing",
                "reason": f"{exc.code}: {exc.message}"[:200],
                "runtime": None,
            }
        result["checked_at"] = checked
        self._probes[key] = (time.monotonic(), result)
        return result

    # --------------------------------------------------------------- port methods
    def _forget(self, target: Target) -> None:
        """A failed SSH call makes any cached 'reachable' probe of this target stale."""
        self._probes.pop(target.fingerprint(), None)

    def _call(self, target: Target, verb: str, request: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.transport.call(target, verb, request)
        except Unreachable as exc:
            self._forget(target)
            raise _unavailable(exc) from None
        except AgentRefused as exc:
            raise _refused(exc) from None

    def allocate(self, spec: dict[str, Any], operation_id: str) -> dict[str, Any]:
        compute = spec.get("compute")
        target = _target(compute)
        reply = self._call(
            target,
            "allocate",
            {
                "operation_id": operation_id,
                "lease_id": spec["lease_id"],
                "generation": int(spec["generation"]),
                "enrollment_key": spec["enrollment_key"],
                "expected_digest": self.expected_digest,
                "path": target.remote_path,
                "cli_overrides": target.cli_overrides,
            },
        )
        return self._result(reply["handle"], compute)

    @staticmethod
    def _result(handle: dict[str, Any], compute: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "operation_id": handle["operation_id"],
            "lease_id": handle["lease_id"],
            "remote_port": handle.get("remote_port"),
            "remote_pid": handle.get("pid"),
            "boot_id": handle.get("boot_id"),
            "lease_dir": handle.get("lease_dir"),
            "resident_machine_id": (compute or {}).get("resident_machine_id"),
            "status": handle["status"],
        }

    def lookup(self, operation_id: str, compute: dict[str, Any] | None) -> dict[str, Any] | None:
        reply = self._call(_target(compute), "lookup", {"operation_id": operation_id})
        if not reply.get("found"):
            return None  # authoritative: the agent holds the operation lock while answering
        return self._result(reply["handle"], compute)

    def describe(self, handle: dict[str, Any], compute: dict[str, Any] | None) -> dict[str, Any]:
        found = self.lookup(handle["operation_id"], compute)
        return {"status": found["status"] if found else "terminated"}

    def connect_runtime(self, handle: dict[str, Any], compute: dict[str, Any] | None) -> str:
        target = _target(compute)
        if not handle.get("remote_port"):
            raise DomainError(
                "executor_unavailable", "resident runtime still starting", retryable=True
            )
        try:
            port = self.tunnels.ensure(handle["lease_id"], handle["remote_port"], lambda: target)
        except Unreachable as exc:
            self._forget(target)
            raise _unavailable(exc) from None
        deadline = time.monotonic() + self.health_timeout
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2).status_code == 200:
                    return (
                        f"{SCHEME}{(compute or {}).get('resident_machine_id')}/{handle['lease_id']}"
                    )
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise DomainError(
            "executor_unavailable", "resident runtime not reachable through SSH", retryable=True
        )

    def endpoint(self, lease: dict[str, Any], target: Callable[[], dict[str, Any]]) -> str:
        """Local URL of the lease's runtime, re-creating the SSH forward when it is gone."""
        handle = lease.get("handle") or {}
        if not handle.get("remote_port"):
            raise RuntimeUnavailable("resident lease has no runtime port")
        resolved: list[Target] = []

        def load() -> Target:
            resolved.append(Target.from_dict(target()))
            return resolved[-1]

        try:
            port = self.tunnels.ensure(lease["id"], handle["remote_port"], load)
        except Unreachable as exc:
            for item in resolved:
                self._forget(item)
            raise RuntimeUnavailable(f"resident machine {exc.status}") from None
        except DomainError as exc:
            raise RuntimeUnavailable(exc.code) from None
        return f"http://127.0.0.1:{port}"

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
        target = _target(compute)
        reply = self._call(target, "terminate", {"operation_id": handle["operation_id"]})
        if reply.get("stopped"):
            self.tunnels.close(handle.get("lease_id") or "")
        return bool(reply.get("stopped"))
