"""First-run setup: ``sbx server init`` writes a private config and prepares PostgreSQL."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg

from control.selfhost import config as cfg
from control.selfhost.postgres import ManagedPostgres

# Entries of an SBX data dir that only exist after a control plane used it.
_INSTANCE_ENTRIES = ("postgres", "blobs", "executor", "git", "mail", "run")


def existing_instance_data(data_dir: Path) -> list[str]:
    if not data_dir.is_dir():
        return []
    return [name for name in _INSTANCE_ENTRIES if (data_dir / name).exists()]


def _has_sbx_schema(dsn: str) -> bool | None:
    """Whether the database already has SBX migrations; None when it cannot be reached."""
    try:
        with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as conn:
            row = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()
            return bool(row and row[0])
    except psycopg.Error:
        return None


def initialize(
    path: Path,
    *,
    data_dir: Path,
    database_url: str | None = None,
    host: str = cfg.DEFAULT_HOST,
    port: int = cfg.DEFAULT_PORT,
    public_url: str | None = None,
    executors: str = "local,modal",
) -> dict[str, Any]:
    """Create a new server config with fresh keys; never replaces an existing one.

    Without ``database_url`` a private PostgreSQL cluster is created under ``data_dir``.
    Secret values are written only to the ``0600`` config file and never returned.
    """
    path = Path(path).expanduser().absolute()
    data_dir = Path(data_dir).expanduser().absolute()
    if path.exists():
        cfg.write_new(path, {})  # raises the actionable "already exists" ConfigError
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    values = {
        "SBX_VAULT_KEYS": cfg.generate_vault_spec(),
        "SBX_RUNTIME_MASTER_KEY": cfg.generate_master_key(),
        "SBX_DATA_DIR": str(data_dir),
        "SBX_LISTEN_HOST": host,
        "SBX_LISTEN_PORT": str(port),
        "SBX_PUBLIC_URL": public_url or url,
        "SBX_EXECUTORS": executors,
    }
    problems = cfg.problems({**values, "SBX_DATABASE_URL": database_url or "pending"})
    if problems:
        key, message, action = problems[0]
        raise cfg.ConfigError(message, action or None)
    warnings: list[str] = []
    existing = existing_instance_data(data_dir)
    if existing:
        # New keys against old data would make every stored credential undecryptable.
        raise cfg.ConfigError(
            f"{data_dir} already holds SBX data ({', '.join(existing)}) created with other keys",
            "use the server config that created it (--config PATH or SBX_SERVER_CONFIG), "
            "or choose an empty --data-dir",
        )
    if database_url:
        has_schema = _has_sbx_schema(database_url)
        if has_schema is None:
            warnings.append(
                "the database could not be reached now; `sbx server check` diagnoses it"
            )
        if has_schema:
            raise cfg.ConfigError(
                "that database already holds an SBX schema created with other keys",
                "use the server config that created it (--config PATH or SBX_SERVER_CONFIG), "
                "or point --external-database at an empty database",
            )
        cfg.private_dir(data_dir)
        values["SBX_DATABASE_URL"] = database_url
        database: dict[str, Any] = {"managed": False}
    else:
        cfg.private_dir(data_dir)
        managed = ManagedPostgres(data_dir)
        managed.init()
        values["SBX_DATABASE_URL"] = managed.dsn()
        values["SBX_MANAGED_POSTGRES"] = "1"
        database = {"managed": True, "socket_dir": str(managed.socket_dir)}
    cfg.write_new(path, values)
    return {
        "config_file": str(path),
        "data_dir": str(data_dir),
        "url": url,
        "database": database,
        "executors": executors.split(","),
        "warnings": warnings,
        "next": [
            "sbx server check",
            "sbx server start",
            f"sbx --base-url {url} auth register --email you@example.com",
        ],
    }
