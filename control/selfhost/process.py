"""Background lifecycle of one self-hosted control plane: start, stop, restart, status.

The server process is ``python -m control.composition serve`` (API and Job workers in one
process) with the effective settings in its environment. Its PID file and log live under
``$SBX_DATA_DIR`` (``run/server.pid``, ``log/server.log``; files ``0600``), so separate
instances with separate config files never share state. Stopping sends SIGTERM and waits:
claimed Jobs of a stopped worker expire and are reclaimed on the next start, and every
durable state stays in PostgreSQL.
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
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from control.selfhost import checks as chk
from control.selfhost.config import ConfigError, ServerSettings, private_dir
from control.selfhost.postgres import ManagedPostgres


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def _start_time(pid: int) -> str | None:
    """Kernel start time of ``pid`` (clock ticks since boot); None where /proc is absent."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _is_instance(pid: int, started: str | None, data_dir: Path, config: Path | None) -> bool:
    """Only signal this instance's control plane, never a recycled or foreign PID.

    On Linux the PID must still have the recorded start time, run ``control.composition``
    and carry this instance's ``SBX_DATA_DIR`` and (unless ``config`` is None)
    ``SBX_SERVER_CONFIG`` in its environment.
    """
    proc = Path(f"/proc/{pid}")
    if not Path("/proc/self").exists():
        return _alive(pid)  # no /proc (macOS): the PID file and liveness are all we have
    try:
        cmdline = (proc / "cmdline").read_bytes()
        environ = (proc / "environ").read_bytes().split(b"\0")
    except OSError:
        return False
    if started is not None and _start_time(pid) != started:
        return False
    return (
        b"control.composition" in cmdline
        and b"SBX_DATA_DIR=" + str(data_dir).encode() in environ
        and (config is None or b"SBX_SERVER_CONFIG=" + str(config).encode() in environ)
    )


class ServerProcess:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings
        self.pidfile = settings.data_dir / "run" / "server.pid"
        self.log = settings.data_dir / "log" / "server.log"
        self.postgres = ManagedPostgres(settings.data_dir) if settings.managed_postgres else None

    # ------------------------------------------------------------------- inspection
    def _recorded(self) -> tuple[int, str | None] | None:
        try:
            fields = self.pidfile.read_text().split()
            return int(fields[0]), (fields[1] if len(fields) > 1 else None)
        except (FileNotFoundError, ValueError, IndexError):
            return None

    def pid(self) -> int | None:
        """This instance's (this config's) running control plane, if any."""
        recorded = self._recorded()
        if recorded is None:
            return None
        pid, started = recorded
        if _alive(pid) and _is_instance(pid, started, self.settings.data_dir, self.settings.path):
            return pid
        return None

    def occupant(self) -> int | None:
        """A live control plane started over this data dir with a different config."""
        recorded = self._recorded()
        if recorded is None or self.pid() is not None:
            return None
        pid, started = recorded
        if _alive(pid) and _is_instance(pid, started, self.settings.data_dir, None):
            return pid
        return None

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize start/stop/restart of this instance across processes."""
        private_dir(self.pidfile.parent)
        with open(self.pidfile.parent / "server.lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def probe(self, timeout: float = 2.0) -> dict[str, Any]:
        """``/healthz`` and ``/readyz`` of the configured URL (no authentication)."""
        url = self.settings.url
        try:
            health = httpx.get(url + "/healthz", timeout=timeout)
            ready = httpx.get(url + "/readyz", timeout=timeout)
        except httpx.HTTPError as exc:
            return {"reachable": False, "error": type(exc).__name__}
        body: dict[str, Any] = {}
        try:
            body = health.json()
        except ValueError:
            pass
        return {
            "reachable": True,
            "sbx": health.status_code == 200 and body.get("ok") is True,
            "git_sha": body.get("git_sha"),
            "ready": ready.status_code == 200,
        }

    def status(self) -> tuple[dict[str, Any], int]:
        pid = self.pid()
        found: list[chk.Check] = []
        if self.postgres is not None:
            pg = self.postgres.describe()
            found.append(
                chk.Check(
                    "managed_postgres",
                    "reachability",
                    "ok" if pg["state"] == "running" else "fail",
                    f"managed PostgreSQL is {pg['state'].replace('_', ' ')}",
                    None if pg["state"] == "running" else "`sbx server start`",
                )
            )
        probe = self.probe() if pid else {"reachable": False}
        if pid is None:
            found.append(
                chk.Check(
                    "server",
                    "reachability",
                    "fail",
                    "the control plane is not running",
                    "`sbx server start` (see `sbx server logs` if it stopped unexpectedly)",
                )
            )
        elif not probe["reachable"]:
            found.append(
                chk.Check(
                    "server",
                    "reachability",
                    "fail",
                    f"process {pid} is running but {self.settings.url} does not answer",
                    "wait for start-up to finish, or see `sbx server logs`",
                )
            )
        elif not probe["ready"]:
            found.append(
                chk.Check(
                    "server",
                    "reachability",
                    "fail",
                    "the control plane answers but cannot read PostgreSQL (/readyz is 503)",
                    "`sbx server check` shows the database problem",
                )
            )
        else:
            found.append(chk.Check("server", "reachability", "ok", f"ready at {self.settings.url}"))
        result = chk.report(
            found,
            state="running" if pid else "stopped",
            pid=pid,
            url=self.settings.url,
            git_sha=probe.get("git_sha"),
            config_file=str(self.settings.path),
            data_dir=str(self.settings.data_dir),
            log=str(self.log),
        )
        return result, chk.exit_code(found)

    def local_runtimes(self) -> int:
        """Live local-executor sbx-runtime daemons of this instance (one per lease)."""
        registry = self.settings.data_dir / "executor" / "registry"
        count = 0
        for record in registry.glob("*.json") if registry.is_dir() else []:
            try:
                pid = int(json.loads(record.read_text()).get("pid") or 0)
            except (OSError, ValueError, AttributeError):
                continue
            if pid and _alive(pid):
                count += 1
        return count

    def tail(self, lines: int = 50) -> list[str]:
        try:
            return self.log.read_text(errors="replace").splitlines()[-lines:]
        except FileNotFoundError:
            return []

    # -------------------------------------------------------------------- lifecycle
    def start(self, *, timeout: float = 90.0) -> tuple[dict[str, Any], int]:
        with self._locked():
            return self._start(timeout=timeout)

    def stop(
        self, *, timeout: float = 30.0, keep_postgres: bool = False
    ) -> tuple[dict[str, Any], int]:
        with self._locked():
            return self._stop(timeout=timeout, keep_postgres=keep_postgres)

    def restart(self, *, timeout: float = 90.0) -> tuple[dict[str, Any], int]:
        with self._locked():
            stopped, code = self._stop(keep_postgres=True)
            if code:
                return stopped, code
            started, code = self._start(timeout=timeout)
            return {**started, "previous_pid": stopped.get("pid")}, code

    def _start(self, *, timeout: float) -> tuple[dict[str, Any], int]:
        pid = self.pid()
        if pid is not None:
            report, code = self.status()
            return {**report, "action_taken": "already_running"}, code
        found = chk.config_checks(self.settings)
        occupant = self.occupant()
        if occupant is not None:
            found.append(
                chk.Check(
                    "data_dir_in_use",
                    "config",
                    "fail",
                    f"{self.settings.data_dir} is in use by a control plane started with another "
                    f"server config (pid {occupant})",
                    "stop it with its own --config, or give this config its own SBX_DATA_DIR",
                )
            )
        if chk.first_failure(found) is None and self.postgres is not None:
            try:
                started_pg = self.postgres.start()
            except ConfigError as exc:
                found.append(
                    chk.Check("managed_postgres", "reachability", "fail", exc.message, exc.action)
                )
            else:
                found.append(
                    chk.Check(
                        "managed_postgres",
                        "reachability",
                        "ok",
                        "started" if started_pg else "already running",
                    )
                )
        if chk.first_failure(found) is None:
            found += [c for c in chk.database_checks(self.settings) if c.name != "managed_postgres"]
        if chk.first_failure(found) is None:
            found.append(chk.port_check(self.settings))
        # Executor problems never block start-up: the API stays usable and Turns report
        # executor_unavailable; the checks are returned so the operator sees them.
        found += chk.executor_checks(self.settings)
        blocking = [c for c in found if c.status == "fail" and c.category != "executor"]
        if blocking:
            return chk.report(found, state="stopped", action_taken="refused"), chk.exit_code(
                blocking
            )
        proc = self._spawn()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.pidfile.unlink(missing_ok=True)
                found.append(
                    chk.Check(
                        "server",
                        "config",
                        "fail",
                        f"the control plane exited during start-up (status {proc.returncode})",
                        f"see {self.log} or `sbx server logs`",
                    )
                )
                return chk.report(
                    found, state="stopped", action_taken="failed", log_tail=self.tail(8)
                ), chk.exit_code([found[-1]])
            probe = self.probe(timeout=1.0)
            if probe["reachable"] and probe.get("ready"):
                found.append(
                    chk.Check("server", "reachability", "ok", f"ready at {self.settings.url}")
                )
                return chk.report(
                    found,
                    state="running",
                    action_taken="started",
                    pid=proc.pid,
                    url=self.settings.url,
                    log=str(self.log),
                ), chk.exit_code([c for c in found if c.category != "executor"])
            time.sleep(0.25)
        self._terminate(proc.pid, timeout=15)
        self.pidfile.unlink(missing_ok=True)
        found.append(
            chk.Check(
                "server",
                "reachability",
                "fail",
                f"the control plane did not become ready within {int(timeout)}s; it was stopped",
                f"see {self.log}",
            )
        )
        return chk.report(found, state="stopped", action_taken="failed", log_tail=self.tail(8)), 4

    def _spawn(self) -> subprocess.Popen[bytes]:
        private_dir(self.settings.data_dir)
        private_dir(self.pidfile.parent)
        private_dir(self.log.parent)
        log_fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(self.log, 0o600)
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "control.composition",
                    "serve",
                    "--host",
                    self.settings.host,
                    "--port",
                    str(self.settings.port),
                ],
                env=self.settings.child_env(),
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                cwd=str(self.settings.data_dir),
                start_new_session=True,
            )
        finally:
            os.close(log_fd)
        fd = os.open(self.pidfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{proc.pid} {_start_time(proc.pid) or ''}".strip() + "\n")
        return proc

    def _terminate(self, pid: int, *, timeout: float) -> bool:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _alive(pid):
                return True
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        time.sleep(0.3)
        return not _alive(pid)

    def _stop(
        self, *, timeout: float = 30.0, keep_postgres: bool = False
    ) -> tuple[dict[str, Any], int]:
        pid = self.pid()
        result: dict[str, Any] = {"ok": True, "state": "stopped", "pid": pid}
        occupant = self.occupant()
        if occupant is not None:
            # Another config's server owns this data dir (and its managed PostgreSQL):
            # leave its PID file and database alone.
            result.update(action_taken="not_running", data_dir_in_use_by=occupant)
            return result, 0
        if pid is None:
            result["action_taken"] = "not_running"
        else:
            graceful = self._terminate(pid, timeout=timeout)
            result["action_taken"] = "stopped" if graceful else "kill_failed"
            if not graceful:
                result.update(ok=False, state="running")
                return result, 4
        self.pidfile.unlink(missing_ok=True)
        runtimes = self.local_runtimes()
        if runtimes:
            # Durable leases: the next start adopts them; idle ones are released after
            # the idle timeout while the control plane runs.
            result["local_runtimes_running"] = runtimes
        if self.postgres is not None:
            if keep_postgres:
                result["postgres"] = "kept_running"
            else:
                try:
                    result["postgres"] = "stopped" if self.postgres.stop() else "not_running"
                except ConfigError as exc:
                    result.update(
                        ok=False,
                        postgres="running",
                        problem={
                            "name": "managed_postgres",
                            "category": "reachability",
                            "status": "fail",
                            "message": exc.message,
                            "action": exc.action,
                        },
                    )
                    return result, 4
        return result, 0
