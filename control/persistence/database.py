"""PostgreSQL connectivity and schema migrations.

Plain psycopg3 sync connections. A tiny bounded pool is enough — every unit of
work takes one connection for one transaction.
"""

from __future__ import annotations

import os
import queue
import re
import threading
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    """Bounded connection pool + migration runner."""

    def __init__(self, dsn: str, *, max_size: int = 8) -> None:
        self.dsn = dsn
        self._pool: queue.Queue[psycopg.Connection] = queue.Queue(max_size)
        self._size = max_size
        self._created = 0
        self._lock = threading.Lock()
        self._closed = False

    def _new_conn(self) -> psycopg.Connection:
        conn = psycopg.connect(self.dsn, autocommit=False, row_factory=dict_row)
        conn.execute("SET application_name = 'sbx-control'")
        return conn

    def acquire(self, timeout: float = 30.0) -> psycopg.Connection:
        if self._closed:
            raise RuntimeError("database closed")
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._created < self._size:
                    self._created += 1
                    return self._new_conn()
        return self._pool.get(timeout=timeout)

    def release(self, conn: psycopg.Connection) -> None:
        if self._closed:
            conn.close()
            return
        if conn.closed:
            with self._lock:
                self._created -= 1
            return
        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            conn.close()
            with self._lock:
                self._created -= 1

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                self._pool.get_nowait().close()
            except queue.Empty:
                return

    # ------------------------------------------------------------------
    @staticmethod
    def migration_files() -> list[tuple[int, Path]]:
        out = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            m = re.match(r"^(\d+)_", path.name)
            if m:
                out.append((int(m.group(1)), path))
        return out

    def migrate(self) -> list[int]:
        """Apply pending migrations in order; each file manages its own
        ``BEGIN``/``COMMIT`` inside an autocommit connection."""
        applied: list[int] = []
        conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version integer PRIMARY KEY, applied_at timestamptz NOT NULL"
                " DEFAULT now(), description text NOT NULL)"
            )
            done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
            for version, path in self.migration_files():
                if version in done:
                    continue
                conn.execute(path.read_text())
                if "schema_migrations" not in path.read_text():
                    conn.execute(
                        "INSERT INTO schema_migrations (version, description) VALUES (%s, %s)",
                        (version, path.name),
                    )
                applied.append(version)
        finally:
            conn.close()
        return applied


def database_url() -> str:
    dsn = os.environ.get("SBX_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("SBX_DATABASE_URL is not set")
    return dsn


def open_database(dsn: str | None = None, *, max_size: int = 8) -> Database:
    db = Database(dsn or database_url(), max_size=max_size)
    db.migrate()
    return db
