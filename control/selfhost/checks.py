"""Categorised start-up checks for a self-hosted control plane (``sbx server check``).

Every finding names one category so an operator can tell the failure classes apart:

* ``config``: a setting is missing or malformed, or a private file is exposed;
* ``reachability``: PostgreSQL (or the listen port) cannot be reached or used;
* ``authentication``: PostgreSQL refused the configured identity;
* ``executor``: no configured executor backend can run Turns on this host.

Checks only read: they never migrate, create data or contact a cloud account.
"""

from __future__ import annotations

import errno
import importlib.util
import os
import shutil
import socket
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psycopg

from control.persistence.database import MIGRATIONS
from control.selfhost import config as cfg
from control.selfhost.postgres import ManagedPostgres, find_bindir

CATEGORIES = ("config", "reachability", "authentication", "executor")
# Exit status per first failing category, shared by `sbx server check|start|status`
# and the client-side `sbx status`.
EXIT_CODES = {"config": 3, "reachability": 4, "authentication": 5, "executor": 6}

_AUTH_SQLSTATES = {"28000", "28P01"}
_AUTH_TEXT = (
    "password authentication failed",
    "peer authentication failed",
    "no pg_hba.conf entry",
    "authentication failed",
    "role ",  # role "x" does not exist
    "pg_hba.conf rejects connection",
)


@dataclass
class Check:
    name: str
    category: str
    status: str  # ok | warn | fail
    message: str
    action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def first_failure(checks: list[Check]) -> Check | None:
    failing = [c for c in checks if c.status == "fail"]
    if not failing:
        return None
    return min(failing, key=lambda c: CATEGORIES.index(c.category))


def exit_code(checks: list[Check]) -> int:
    failure = first_failure(checks)
    return 0 if failure is None else EXIT_CODES[failure.category]


def report(checks: list[Check], **extra: Any) -> dict[str, Any]:
    failure = first_failure(checks)
    return {
        "ok": failure is None,
        **extra,
        "problem": failure.to_dict() if failure else None,
        "checks": [c.to_dict() for c in checks],
    }


# ------------------------------------------------------------------------------ config
def config_checks(settings: cfg.ServerSettings) -> list[Check]:
    out: list[Check] = []
    path = settings.path
    if settings.file_exists:
        mode = cfg.mode_of(path)
        if mode is not None and mode & 0o077:
            out.append(
                Check(
                    "config_file",
                    "config",
                    "fail",
                    f"{path} is readable by other users (mode {mode:04o}); it holds the vault keys",
                    f"chmod 600 {path}",
                )
            )
        else:
            out.append(Check("config_file", "config", "ok", f"{path} (mode 0600)"))
        parent = cfg.mode_of(path.parent)
        if parent is not None and parent & 0o077:
            out.append(
                Check(
                    "config_dir",
                    "config",
                    "warn",
                    f"{path.parent} is accessible by other users (mode {parent:04o})",
                    f"chmod 700 {path.parent}",
                )
            )
    else:
        env_only = all(settings.sources.get(k) == "environment" for k in cfg.REQUIRED_KEYS)
        out.append(
            Check(
                "config_file",
                "config",
                "warn" if env_only else "fail",
                f"no server config at {path}"
                + ("; using environment variables" if env_only else ""),
                None if env_only else "run `sbx server init`",
            )
        )
    found = cfg.problems(settings.values)
    for key, message, action in found:
        out.append(
            Check(key.lower().removeprefix("sbx_"), "config", "fail", message, action or None)
        )
    if not any(k in cfg.REQUIRED_KEYS for k, _, _ in found):
        out.append(
            Check("required_settings", "config", "ok", "database, vault and runtime keys set")
        )
    out.append(_data_dir_check(settings.data_dir))
    return out


def _data_dir_check(data_dir: Path) -> Check:
    probe = data_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not os.access(probe, os.W_OK | os.X_OK):
        return Check(
            "data_dir",
            "config",
            "fail",
            f"{data_dir} is not writable by this user",
            "set SBX_DATA_DIR to a directory you own",
        )
    return Check("data_dir", "config", "ok", str(data_dir))


# ------------------------------------------------------------------------ PostgreSQL
def classify_connect_error(exc: Exception) -> tuple[str, str]:
    """``(category, action)`` for a failed PostgreSQL connection."""
    sqlstate = getattr(exc, "sqlstate", None)
    text = str(exc).lower()
    if sqlstate in _AUTH_SQLSTATES or any(t in text for t in _AUTH_TEXT):
        return (
            "authentication",
            "check the user and password in SBX_DATABASE_URL and the server's pg_hba.conf",
        )
    if sqlstate == "3D000" or ("database" in text and "does not exist" in text):
        return ("config", "create the database or fix its name in SBX_DATABASE_URL")
    if isinstance(exc, psycopg.ProgrammingError) or "invalid dsn" in text or 'missing "="' in text:
        return ("config", "SBX_DATABASE_URL is not a valid PostgreSQL connection string")
    return (
        "reachability",
        "start PostgreSQL or fix the host/port in SBX_DATABASE_URL (`sbx server start` "
        "starts a managed cluster)",
    )


def _redact_dsn_text(text: str, dsn: str) -> str:
    """libpq's reason (its FATAL line) on one line; the DSN itself is never echoed.

    libpq messages name the host or socket but never the password.
    """
    lines = [line.strip() for line in text.replace(dsn, "SBX_DATABASE_URL").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    reason = next((line for line in lines if line.startswith(("FATAL:", "ERROR:"))), None)
    head = lines[0].removeprefix("connection failed: ")
    return f"{head} {reason}" if reason and reason not in head else head


def database_checks(settings: cfg.ServerSettings, *, timeout: int = 5) -> list[Check]:
    out: list[Check] = []
    managed = ManagedPostgres(settings.data_dir) if settings.managed_postgres else None
    if managed is not None:
        if find_bindir() is None:
            out.append(
                Check(
                    "postgres_binaries",
                    "config",
                    "fail",
                    "PostgreSQL server binaries (initdb, pg_ctl) were not found",
                    "install the PostgreSQL server package",
                )
            )
            return out
        if not managed.initialized():
            out.append(
                Check(
                    "managed_postgres",
                    "config",
                    "fail",
                    f"the managed PostgreSQL cluster {managed.data} does not exist",
                    "run `sbx server init`",
                )
            )
            return out
        if not managed.running():
            out.append(
                Check(
                    "managed_postgres",
                    "reachability",
                    "warn",
                    "the managed PostgreSQL cluster is stopped",
                    "`sbx server start` starts it",
                )
            )
            return out
        out.append(Check("managed_postgres", "reachability", "ok", "running (Unix socket only)"))
    dsn = settings.values.get("SBX_DATABASE_URL")
    if not dsn:
        return out
    try:
        with psycopg.connect(dsn, connect_timeout=timeout, autocommit=True) as conn:
            version = conn.info.server_version
            applied = _applied_migrations(conn)
    except Exception as exc:  # psycopg raises several classes for one cause
        category, action = classify_connect_error(exc)
        out.append(
            Check(
                "database",
                category,
                "fail",
                "cannot connect to PostgreSQL: " + _redact_dsn_text(str(exc), dsn),
                action,
            )
        )
        return out
    major = version // 10000
    out.append(
        Check(
            "database",
            "reachability",
            "ok" if major >= 15 else "fail",
            f"PostgreSQL {major}" + ("" if major >= 15 else " is older than the required 15"),
            None if major >= 15 else "use PostgreSQL 15 or newer",
        )
    )
    files = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    pending = [name for name in files if name not in applied]
    unknown = sorted(set(applied) - set(files))
    if unknown:
        out.append(
            Check(
                "migrations",
                "config",
                "fail",
                f"the database has {len(unknown)} migration(s) this version does not know "
                f"(newest {unknown[-1]})",
                "run the SBX version that created this database, or point SBX_DATABASE_URL "
                "at another database",
            )
        )
    elif pending:
        out.append(
            Check(
                "migrations",
                "config",
                "warn",
                f"{len(pending)} migration(s) pending",
                "`sbx server start` (or `sbx migrate`) applies them",
            )
        )
    else:
        out.append(Check("migrations", "config", "ok", f"{len(files)} applied"))
    return out


def _applied_migrations(conn: psycopg.Connection) -> set[str]:
    exists = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()
    if not exists or not exists[0]:
        return set()
    return {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}


# -------------------------------------------------------------------------- executors
# Default official CLI command per Harness (runtime/harnesses/*.py ``cli_bin``); the local
# executor runs them from the host PATH with a per-lease HOME.
LOCAL_HARNESS_CLIS = {
    "opencode": "opencode",
    "codex": "codex",
    "claude": "claude",
    "grok": "grok",
    "commandcode": "cmd",
    "antigravity": "agy",
}


def executor_checks(settings: cfg.ServerSettings) -> list[Check]:
    out: list[Check] = []
    for name in settings.executors:
        if name == "local":
            out.append(_local_executor_check())
            if out[-1].status == "ok":
                out.append(_local_cli_check())
        elif name == "modal":
            if importlib.util.find_spec("modal") is None:
                out.append(
                    Check(
                        "executor_modal",
                        "executor",
                        "fail",
                        "the modal package is not installed",
                        "reinstall SBX with its locked dependencies (`uv sync --frozen`)",
                    )
                )
            else:
                out.append(
                    Check(
                        "executor_modal",
                        "executor",
                        "ok",
                        "available; each workspace needs a verified Modal Connection",
                    )
                )
    usable = [c for c in out if c.name.startswith("executor_") and c.status == "ok"]
    if out and not usable:
        out.append(
            Check(
                "executors",
                "executor",
                "fail",
                "no configured executor backend can run Turns on this host",
                "enable a usable backend in SBX_EXECUTORS",
            )
        )
    return out


def _local_cli_check() -> Check:
    found = [p for p, binary in LOCAL_HARNESS_CLIS.items() if shutil.which(binary)]
    missing = [p for p in LOCAL_HARNESS_CLIS if p not in found]
    if not missing:
        return Check("local_harness_clis", "executor", "ok", "all official CLIs on PATH")
    return Check(
        "local_harness_clis",
        "executor",
        "warn",
        f"official CLIs on PATH: {', '.join(found) or 'none'}; not found: {', '.join(missing)}",
        "install the CLI a local Session selects (e.g. `npm install -g opencode-ai` for the "
        "default OpenCode Harness); without it the Turn fails with spawn_failed",
    )


def _local_executor_check() -> Check:
    # sbx-runtime uses Linux process supervision (prctl subreaper, /proc).
    if not sys.platform.startswith("linux") or not Path("/proc/self/stat").exists():
        return Check(
            "executor_local",
            "executor",
            "warn",
            f"the local executor needs Linux; this host is {sys.platform}",
            "use the modal executor here, or run the control plane on Linux",
        )
    if importlib.util.find_spec("runtime.daemon.main") is None:
        return Check(
            "executor_local",
            "executor",
            "warn",
            "the sbx-runtime package is not importable",
            "run from a source checkout or a full SBX install",
        )
    if shutil.which("git") is None:
        return Check(
            "executor_local",
            "executor",
            "warn",
            "git is not on PATH; Worktrees need it",
            "install git",
        )
    return Check(
        "executor_local",
        "executor",
        "ok",
        "available (single-user development executor, not a multi-tenant sandbox)",
    )


# ------------------------------------------------------------------------------- port
def port_check(settings: cfg.ServerSettings) -> Check:
    host, port = settings.host, settings.port
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return Check(
                    "listen_port",
                    "config",
                    "fail",
                    f"{host}:{port} is already in use by another process",
                    "stop that process or set SBX_LISTEN_PORT (or --port)",
                )
            return Check(
                "listen_port",
                "config",
                "fail",
                f"cannot listen on {host}:{port}: {exc.strerror}",
                "set SBX_LISTEN_HOST/SBX_LISTEN_PORT",
            )
    return Check("listen_port", "config", "ok", f"{host}:{port} is free")


def run_all(settings: cfg.ServerSettings, *, include_port: bool = True) -> list[Check]:
    checks = config_checks(settings)
    checks += database_checks(settings)
    checks += executor_checks(settings)
    if include_port:
        checks.append(port_check(settings))
    return checks
