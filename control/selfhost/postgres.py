"""Optional private PostgreSQL cluster for a single-operator control plane.

``sbx server init`` (without ``--external-database``) creates a cluster from the
installed PostgreSQL server binaries inside ``$SBX_DATA_DIR/postgres``. It listens on a
Unix socket only, in a ``0700`` directory, and authenticates by ``peer`` as the current OS
user; TCP connections are rejected. It is ordinary PostgreSQL: the business authority,
migrations and backups work exactly as with an external server.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
from pathlib import Path

import psycopg

from control.selfhost.config import ConfigError, current_user, private_dir

PORT = 5432
DATABASE = "sbx"
# sockaddr_un.sun_path is 108 bytes on Linux (104 on macOS) including the NUL.
_SOCKET_PATH_MAX = 103


def find_bindir() -> Path | None:
    """The newest installed PostgreSQL server binaries (Debian/Ubuntu layout, then PATH)."""
    for candidate in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
        if Path(candidate, "initdb").exists() and Path(candidate, "pg_ctl").exists():
            return Path(candidate)
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if initdb and pg_ctl and Path(initdb).parent == Path(pg_ctl).parent:
        return Path(initdb).parent
    return None


class ManagedPostgres:
    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "postgres"
        self.data = self.root / "data"
        self.socket_dir = self.root / "run"
        self.log = Path(data_dir) / "log" / "postgres.log"

    # ------------------------------------------------------------------- inspection
    def dsn(self, user: str | None = None) -> str:
        return psycopg.conninfo.make_conninfo(
            host=str(self.socket_dir), port=str(PORT), user=user or current_user(), dbname=DATABASE
        )

    def initialized(self) -> bool:
        return (self.data / "PG_VERSION").exists()

    def socket_path_error(self) -> str | None:
        path = self.socket_dir / f".s.PGSQL.{PORT}"
        if len(str(path)) > _SOCKET_PATH_MAX:
            return (
                f"the PostgreSQL socket path {path} is longer than the {_SOCKET_PATH_MAX} bytes "
                "a Unix socket allows"
            )
        return None

    def _bindir(self) -> Path:
        bindir = find_bindir()
        if bindir is None:
            raise ConfigError(
                "PostgreSQL server binaries (initdb, pg_ctl) were not found",
                "install the PostgreSQL server package (Ubuntu: `sudo apt install postgresql`; "
                "macOS: `brew install postgresql@17`), or use --external-database",
            )
        return bindir

    def running(self) -> bool:
        if not self.initialized():
            return False
        bindir = find_bindir()
        if bindir is None:
            return False
        result = subprocess.run(
            [str(bindir / "pg_ctl"), "-D", str(self.data), "status"],
            capture_output=True,
            env=_env(),
        )
        return result.returncode == 0

    def describe(self) -> dict[str, object]:
        if not self.initialized():
            state = "not_initialized"
        else:
            state = "running" if self.running() else "stopped"
        return {"managed": True, "state": state, "data": str(self.data)}

    # ------------------------------------------------------------------- lifecycle
    def init(self) -> None:
        """Create the cluster and the ``sbx`` database; idempotent for an existing cluster."""
        if os.geteuid() == 0:
            raise ConfigError(
                "PostgreSQL refuses to run as root",
                "run `sbx server` commands as an ordinary user",
            )
        error = self.socket_path_error()
        if error:
            raise ConfigError(error, "choose a shorter --data-dir")
        bindir = self._bindir()
        private_dir(self.root)
        private_dir(self.socket_dir)
        private_dir(self.log.parent)
        if not self.initialized():
            result = subprocess.run(
                [
                    str(bindir / "initdb"),
                    "-D",
                    str(self.data),
                    "-U",
                    current_user(),
                    "--auth-local=peer",
                    "--auth-host=reject",
                    "-E",
                    "UTF8",
                    "--locale=C",
                ],
                capture_output=True,
                text=True,
                env=_env(),
            )
            if result.returncode != 0:
                raise ConfigError(
                    "initdb failed: " + (result.stderr.strip().splitlines() or ["unknown"])[-1],
                    f"inspect or remove {self.data} and run `sbx server init` again",
                )
            with open(self.data / "postgresql.conf", "a") as fh:
                fh.write(
                    "\n# sbx server: private Unix socket only\n"
                    "listen_addresses = ''\n"
                    f"port = {PORT}\n"
                    f"unix_socket_directories = '{_conf_quote(self.socket_dir)}'\n"
                    "unix_socket_permissions = 0700\n"
                )
        was_running = self.running()
        self.start()
        try:
            with psycopg.connect(
                psycopg.conninfo.make_conninfo(self.dsn(), dbname="postgres"), autocommit=True
            ) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,)
                ).fetchone()
                if not exists:
                    conn.execute(f'CREATE DATABASE "{DATABASE}"')
        finally:
            if not was_running:
                self.stop()

    def start(self, timeout: int = 60) -> bool:
        """Start the cluster; returns False when it was already running."""
        if not self.initialized():
            raise ConfigError(
                f"the managed PostgreSQL cluster {self.data} does not exist",
                "run `sbx server init`",
            )
        if self.running():
            return False
        bindir = self._bindir()
        private_dir(self.socket_dir)
        private_dir(self.log.parent)
        result = subprocess.run(
            [
                str(bindir / "pg_ctl"),
                "-D",
                str(self.data),
                "-l",
                str(self.log),
                "-w",
                "-t",
                str(timeout),
                "start",
            ],
            capture_output=True,
            text=True,
            env=_env(),
        )
        if self.log.exists():
            os.chmod(self.log, 0o600)
        if result.returncode != 0:
            raise ConfigError(
                "the managed PostgreSQL cluster did not start",
                f"see {self.log}",
            )
        return True

    def stop(self, timeout: int = 60) -> bool:
        """Stop the cluster (fast shutdown); returns False when it was not running."""
        if not self.running():
            return False
        bindir = self._bindir()
        result = subprocess.run(
            [
                str(bindir / "pg_ctl"),
                "-D",
                str(self.data),
                "-m",
                "fast",
                "-w",
                "-t",
                str(timeout),
                "stop",
            ],
            capture_output=True,
            text=True,
            env=_env(),
        )
        if result.returncode != 0 and self.running():
            raise ConfigError(
                "the managed PostgreSQL cluster did not stop: "
                + (result.stderr.strip().splitlines() or ["unknown error"])[-1],
                f"retry `sbx server stop`, or see {self.log}",
            )
        return True


def _env() -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    if os.environ.get("HOME"):
        env["HOME"] = os.environ["HOME"]
    return env


def _conf_quote(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace("'", "''")
