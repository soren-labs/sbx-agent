"""Ephemeral real PostgreSQL for tests (RFC 04: PG tests exercise real locking).

One throwaway cluster per pytest session; each test gets a fresh database
cloned from a migrated template. ``SBX_TEST_DATABASE_URL`` (an admin DSN)
uses an existing server instead.
"""

from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from control.persistence.database import Database

TEMPLATE = "sbx_template"


def _pg_bin() -> Path | None:
    for candidate in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
        if Path(candidate, "initdb").exists():
            return Path(candidate)
    found = shutil.which("initdb")
    return Path(found).parent if found else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _dsn(admin: str, dbname: str) -> str:
    return psycopg.conninfo.make_conninfo(admin, dbname=dbname)


@pytest.fixture(scope="session")
def pg_admin_dsn() -> Iterator[str]:
    external = os.environ.get("SBX_TEST_DATABASE_URL")
    if external:
        _prepare_template(external)
        yield external
        return
    bindir = _pg_bin()
    if bindir is None:
        pytest.skip("PostgreSQL binaries not available; set SBX_TEST_DATABASE_URL")
    base = Path(tempfile.mkdtemp(prefix="sbxpg-"))
    data, sock = base / "data", base / "sock"
    sock.mkdir()
    prefix: list[str] = []
    if os.geteuid() == 0:
        shutil.chown(base, "postgres")
        shutil.chown(sock, "postgres")
        prefix = ["runuser", "-u", "postgres", "--"]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    subprocess.run(
        [
            *prefix,
            str(bindir / "initdb"),
            "-D",
            str(data),
            "-A",
            "trust",
            "-U",
            "sbx",
            "-E",
            "UTF8",
            "--no-sync",
        ],
        check=True,
        capture_output=True,
        env=env,
    )
    port = _free_port()
    opts = (
        f"-k {sock} -p {port} -c listen_addresses='' -c fsync=off -c synchronous_commit=off "
        "-c full_page_writes=off -c max_connections=300"
    )
    subprocess.run(
        [
            *prefix,
            str(bindir / "pg_ctl"),
            "-D",
            str(data),
            "-o",
            opts,
            "-l",
            str(base / "log"),
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
        env=env,
    )
    admin = f"host={sock} port={port} user=sbx dbname=postgres"
    try:
        _prepare_template(admin)
        yield admin
    finally:
        subprocess.run(
            [*prefix, str(bindir / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
            capture_output=True,
            env=env,
        )
        shutil.rmtree(base, ignore_errors=True)


def _prepare_template(admin: str) -> None:
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEMPLATE} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {TEMPLATE}")
    db = Database(_dsn(admin, TEMPLATE))
    db.migrate()
    db.close()


def fresh_database(admin: str) -> tuple[str, str]:
    name = "t_" + uuid.uuid4().hex[:16]
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name} TEMPLATE {TEMPLATE}")
    return name, _dsn(admin, name)


def drop_database(admin: str, name: str) -> None:
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def db(pg_admin_dsn: str) -> Iterator[Database]:
    name, dsn = fresh_database(pg_admin_dsn)
    database = Database(dsn, max_connections=32)
    try:
        yield database
    finally:
        database.close()
        drop_database(pg_admin_dsn, name)
