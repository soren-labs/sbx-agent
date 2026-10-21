"""Resident-machine agent: the only program the control plane runs over SSH.

``python -m runtime.resident.agent [--state-root DIR] [--forced] VERB`` reads one JSON
request on stdin and writes one JSON reply on stdout. The verbs are a closed set:

* ``probe``: what this machine runs (runtime source digest, Python, boot id, which
  official CLIs are on the configured PATH, live leases). Read-only.
* ``allocate``: start (or adopt) the official sbx-runtime daemon for one lease.
* ``lookup``: the allocation made under an operation ID, if any.
* ``terminate``: stop exactly the processes owned by that allocation.

There is no generic exec: requests carry identifiers, the per-lease key and a PATH, never
a command. Under an ``authorized_keys`` forced command (``--forced``) the verb is taken
from ``SSH_ORIGINAL_COMMAND`` and must be one of the four.

Allocation is idempotent by operation ID. Under a per-operation ``flock`` the agent writes
an intent record, starts the daemon detached from the SSH session (its own session and
process group, stdio on a log file), records pid + kernel start time + boot id, and only
then waits for readiness, so a lost SSH response, a killed controller or a retry adopts
the same daemon instead of starting a twin. A process is ours only when its pid, start
time and boot id match the record and its command line names the lease's state dir;
signals go through a pidfd opened and re-verified first, never to a bare pid.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

AGENT_VERSION = 1
VERBS = ("probe", "allocate", "lookup", "terminate")
# Runtime sources whose digest identifies what this machine runs; the control plane
# computes the same digest over its own checkout and refuses a mismatch.
DIGEST_PACKAGES = (
    "protocol",
    "runtime/daemon",
    "runtime/harnesses",
    "runtime/resident",
    "runtime/security",
    "runtime/subscriptions",
)
# Official CLI command overrides an operator may pin per machine (absolute paths only).
BIN_OVERRIDES = frozenset(
    {"OPENCODE_BIN", "CODEX_BIN", "CLAUDE_BIN", "GROK_BIN", "COMMANDCODE_BIN", "AGY_BIN"}
)
HARNESS_BINS = {
    "opencode": ("OPENCODE_BIN", "opencode"),
    "codex": ("CODEX_BIN", "codex"),
    "claude": ("CLAUDE_BIN", "claude"),
    "grok": ("GROK_BIN", "grok"),
    "commandcode": ("COMMANDCODE_BIN", "cmd"),
    "antigravity": ("AGY_BIN", "agy"),
}
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
START_TIMEOUT = 20.0
LOCK_TIMEOUT = 30.0
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$")
ROOT = Path(__file__).resolve().parents[2]


class AgentError(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


# ------------------------------------------------------------------------------ identity
def source_digest(root: Path = ROOT) -> str:
    parts = []
    for package in DIGEST_PACKAGES:
        for path in sorted((root / package).rglob("*.py")):
            parts.append(hashlib.sha256(path.read_bytes()).hexdigest())
    return hashlib.sha256("".join(parts).encode()).hexdigest()[:32]


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def _stat(pid: int) -> dict[str, Any] | None:
    try:
        data = Path(f"/proc/{pid}/stat").read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    rest = data[data.rindex(")") + 2 :].split()
    return {"state": rest[0], "ppid": int(rest[1]), "start": int(rest[19])}


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _own_pids() -> list[int]:
    uid = os.getuid()
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid == uid:
                pids.append(int(entry))
        except OSError:
            continue
    return pids


def _is_daemon_for(pid: int, lease_dir: str) -> bool:
    argv = _cmdline(pid)
    return "runtime.daemon.main" in argv and str(Path(lease_dir) / "state") in argv


def _alive(stat: dict[str, Any] | None) -> bool:
    return stat is not None and stat["state"] not in ("Z", "X")


def _owned(record: dict[str, Any]) -> bool:
    """The recorded daemon is alive and is still exactly the process we started."""
    pid = record.get("pid")
    if not pid or record.get("boot_id") != boot_id():
        return False
    stat = _stat(pid)
    return (
        _alive(stat)
        and stat["start"] == record.get("start")
        and _is_daemon_for(pid, record["lease_dir"])
    )


def _find_daemon(lease_dir: str) -> tuple[int, int] | None:
    """A daemon for this lease dir started by an agent that died before recording it."""
    for pid in _own_pids():
        stat = _stat(pid)
        if _alive(stat) and _is_daemon_for(pid, lease_dir):
            return pid, stat["start"]
    return None


# ------------------------------------------------------------------------------- state
class State:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.registry = root / "registry"
        self.leases = root / "leases"

    def ensure(self) -> None:
        for path in (self.root, self.registry, self.leases):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)

    def record_path(self, operation_id: str) -> Path:
        return self.registry / f"{operation_id}.json"

    def read(self, operation_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self.record_path(operation_id).read_text())
        except FileNotFoundError:
            return None

    def write(self, record: dict[str, Any]) -> None:
        path = self.record_path(record["operation_id"])
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(record, fh)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)

    def remove(self, operation_id: str) -> None:
        self.record_path(operation_id).unlink(missing_ok=True)

    @contextlib.contextmanager
    def lock(self, operation_id: str, timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
        fd = os.open(self.registry / f"{operation_id}.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise AgentError(
                            "busy", "another agent call holds this operation; retry"
                        ) from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)


def default_state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "sbx-resident"


def _ident(request: dict[str, Any], name: str) -> str:
    value = request.get(name)
    if not isinstance(value, str) or not _ID.match(value):
        raise AgentError("validation_failed", f"{name} is missing or malformed")
    return value


def _search_path(request: dict[str, Any]) -> str:
    value = request.get("path") or DEFAULT_PATH
    dirs = value.split(":") if isinstance(value, str) else []
    if not dirs or not all(d.startswith("/") and "\n" not in d for d in dirs):
        raise AgentError("validation_failed", "path must be a list of absolute directories")
    return value


def _overrides(request: dict[str, Any]) -> dict[str, str]:
    raw = request.get("cli_overrides") or {}
    if not isinstance(raw, dict):
        raise AgentError("validation_failed", "cli_overrides must be an object")
    out = {}
    for key, value in raw.items():
        if key not in BIN_OVERRIDES or not isinstance(value, str) or not value.startswith("/"):
            raise AgentError("validation_failed", f"cli override {key} is not allowed")
        out[key] = value
    return out


def _handle(record: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "operation_id": record["operation_id"],
        "lease_id": record["lease_id"],
        "remote_port": record.get("port"),
        "pid": record.get("pid"),
        "boot_id": record.get("boot_id"),
        "lease_dir": record["lease_dir"],
        "status": status,
    }


# ---------------------------------------------------------------------------- verbs
def probe(state: State, request: dict[str, Any]) -> dict[str, Any]:
    path = _search_path(request)
    harnesses = {}
    for name, (env_name, default) in HARNESS_BINS.items():
        override = _overrides(request).get(env_name)
        harnesses[name] = override if override else shutil.which(default, path=path)
    live = 0
    if state.registry.is_dir():
        for item in state.registry.glob("*.json"):
            try:
                if _owned(json.loads(item.read_text())):
                    live += 1
            except (OSError, ValueError, KeyError):
                continue
    return {
        "agent_version": AGENT_VERSION,
        "runtime_digest": source_digest(),
        "python": sys.version.split()[0],
        "boot_id": boot_id(),
        "harnesses": {k: bool(v) for k, v in harnesses.items()},
        "live_leases": live,
        "pidfd": hasattr(os, "pidfd_open"),
    }


def _lookup_locked(state: State, operation_id: str) -> dict[str, Any] | None:
    record = state.read(operation_id)
    if record is None:
        return None
    if record.get("pid") is None and record.get("boot_id") == boot_id():
        found = _find_daemon(record["lease_dir"])
        if found:
            record["pid"], record["start"] = found
            state.write(record)
    if not _owned(record):
        return _handle(record, "terminated")
    if record.get("port") is None:
        port_file = Path(record["lease_dir"]) / "port"
        if port_file.exists():
            record["port"] = int(port_file.read_text())
            state.write(record)
    return _handle(record, "running")


def lookup(state: State, request: dict[str, Any]) -> dict[str, Any]:
    operation_id = _ident(request, "operation_id")
    state.ensure()
    with state.lock(operation_id):
        found = _lookup_locked(state, operation_id)
    return {"found": found is not None, "handle": found}


def _daemon_env(request: dict[str, Any], lease_dir: Path, key: str, digest: str) -> dict[str, str]:
    """Allow-listed environment: nothing of the SSH user's HOME, shell or credentials."""
    return {
        "PATH": _search_path(request),
        "HOME": str(lease_dir / "home"),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(ROOT),
        "SBX_RUNTIME_KEY": key,
        "SBX_LEASE_ID": request["lease_id"],
        "SBX_LEASE_GENERATION": str(int(request["generation"])),
        "SBX_IMAGE_DIGEST": f"resident:{digest}",
        **_overrides(request),
    }


def allocate(state: State, request: dict[str, Any]) -> dict[str, Any]:
    operation_id = _ident(request, "operation_id")
    lease_id = _ident(request, "lease_id")
    key = request.get("enrollment_key")
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise AgentError("validation_failed", "enrollment_key is missing or malformed")
    if not isinstance(request.get("generation"), int):
        raise AgentError("validation_failed", "generation must be an integer")
    digest = source_digest()
    expected = request.get("expected_digest")
    if expected and expected != digest:
        raise AgentError(
            "runtime_mismatch",
            "the runtime checkout on this machine differs from the control plane's",
            runtime_digest=digest,
            expected_digest=expected,
        )
    state.ensure()
    with state.lock(operation_id):
        existing = _lookup_locked(state, operation_id)
        if existing is not None:
            return {"handle": existing}
        lease_dir = state.leases / lease_id
        for sub in ("", "home", "state", "work"):
            (lease_dir / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(lease_dir, 0o700)
        port_file = lease_dir / "port"
        port_file.unlink(missing_ok=True)
        record: dict[str, Any] = {
            "operation_id": operation_id,
            "lease_id": lease_id,
            "generation": request["generation"],
            "lease_dir": str(lease_dir),
            "boot_id": boot_id(),
            "pid": None,
            "start": None,
            "port": None,
            "created_at": time.time(),
        }
        # Intent first: a daemon whose agent dies right after spawn is still found by
        # its lease dir and adopted, never duplicated.
        state.write(record)
        env = _daemon_env(request, lease_dir, key, digest)
        log_fd = os.open(lease_dir / "daemon.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-s",
                    "-m",
                    "runtime.daemon.main",
                    "--state-dir",
                    str(lease_dir / "state"),
                    "--work-dir",
                    str(lease_dir / "work"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--port-file",
                    str(port_file),
                ],
                env=env,
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            os.close(log_fd)
        stat = _stat(proc.pid)
        record["pid"], record["start"] = proc.pid, stat["start"] if stat else None
        state.write(record)
        deadline = time.monotonic() + START_TIMEOUT
        while not port_file.exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                if _terminate_record(record):
                    state.remove(operation_id)
                raise AgentError(
                    "start_failed",
                    f"the runtime did not start; see {lease_dir / 'daemon.log'} on the machine",
                )
            time.sleep(0.05)
        record["port"] = int(port_file.read_text())
        state.write(record)
        return {"handle": _handle(record, "running")}


def _targets(record: dict[str, Any]) -> dict[int, int]:
    """pid -> start time of every process owned by this allocation."""
    if record.get("boot_id") != boot_id():
        return {}  # a reboot ended everything the record describes
    lease_dir = record["lease_dir"]
    daemon_start = record.get("start") or 0
    exclude = {os.getpid(), os.getppid()}
    stats = {pid: _stat(pid) for pid in _own_pids() if pid not in exclude}
    stats = {pid: st for pid, st in stats.items() if _alive(st)}
    found: dict[int, int] = {}
    if _owned(record):
        found[record["pid"]] = record["start"]
        children: dict[int, list[int]] = {}
        for pid, st in stats.items():
            children.setdefault(st["ppid"], []).append(pid)
        queue = [record["pid"]]
        while queue:
            for child in children.get(queue.pop(), []):
                if child not in found:
                    found[child] = stats[child]["start"]
                    queue.append(child)
        return found
    if record.get("pid") is None:
        adopted = _find_daemon(lease_dir)
        if adopted:
            found[adopted[0]] = adopted[1]
    # The daemon is gone: its leftovers (Harness CLIs, terminals, services) still run in
    # the lease's worktree or name its state dir, and none predates the daemon.
    prefixes = tuple(str(Path(lease_dir) / sub) + "/" for sub in ("work", "state"))
    for pid, st in stats.items():
        if pid in found or st["start"] < daemon_start:
            continue
        cwd = (_cwd(pid) or "") + "/"
        if cwd.startswith(prefixes) or any(arg.startswith(prefixes) for arg in _cmdline(pid)):
            found[pid] = st["start"]
    return found


def _exited(fd: int) -> bool:
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return bool(poller.poll(0))


def _terminate_record(record: dict[str, Any]) -> bool:
    """Signal only processes verified through a pidfd; True when all of them are gone."""
    fds: dict[int, int] = {}
    for pid, start in _targets(record).items():
        try:
            fd = os.pidfd_open(pid)
        except (ProcessLookupError, OSError):
            continue
        stat = _stat(pid)
        if not _alive(stat) or stat["start"] != start:
            os.close(fd)  # exited or recycled between the scan and the open
            continue
        fds[pid] = fd
    try:
        for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
            for fd in fds.values():
                if not _exited(fd):
                    with contextlib.suppress(ProcessLookupError):
                        signal.pidfd_send_signal(fd, sig)
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and not all(_exited(fd) for fd in fds.values()):
                time.sleep(0.05)
            if all(_exited(fd) for fd in fds.values()):
                return True
        return False
    finally:
        for fd in fds.values():
            os.close(fd)


def terminate(state: State, request: dict[str, Any]) -> dict[str, Any]:
    operation_id = _ident(request, "operation_id")
    state.ensure()
    with state.lock(operation_id):
        _lookup_locked(state, operation_id)  # adopts a daemon an interrupted allocate left
        record = state.read(operation_id)
        if record is None:
            return {"stopped": True, "status": "absent"}
        stopped = _terminate_record(record)
        if stopped:
            record["terminated_at"] = record.get("terminated_at") or time.time()
            state.write(record)
        return {"stopped": stopped, "status": "terminated" if stopped else "running"}


HANDLERS = {"probe": probe, "allocate": allocate, "lookup": lookup, "terminate": terminate}


def _verb(args: argparse.Namespace) -> str:
    if args.forced:
        tokens = shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
        verb = tokens[-1] if tokens else ""
    else:
        verb = args.verb or ""
    if verb not in VERBS:
        raise AgentError("validation_failed", "unknown verb")
    return verb


def main(argv: list[str] | None = None) -> int:
    # The SSH session may go away mid-call; finish the effect and its record anyway.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    parser = argparse.ArgumentParser(prog="sbx-resident-agent")
    parser.add_argument("--state-root")
    parser.add_argument("--forced", action="store_true")
    parser.add_argument("verb", nargs="?")
    args = parser.parse_args(argv)
    try:
        if not sys.platform.startswith("linux") or not hasattr(os, "pidfd_open"):
            raise AgentError("unsupported_platform", "resident machines must run Linux >= 5.3")
        verb = _verb(args)
        raw = sys.stdin.read(65536)
        request = json.loads(raw) if raw.strip() else {}
        if not isinstance(request, dict):
            raise AgentError("validation_failed", "request must be a JSON object")
        root = Path(args.state_root).expanduser() if args.state_root else default_state_root()
        reply = {"ok": True, **HANDLERS[verb](State(root), request)}
    except AgentError as exc:
        reply = {
            "ok": False,
            "error": {"code": exc.code, "message": exc.message, "details": exc.details},
        }
    except Exception as exc:  # never echo request content (it may hold the lease key)
        reply = {"ok": False, "error": {"code": "agent_error", "message": type(exc).__name__}}
    with contextlib.suppress(BrokenPipeError):
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
