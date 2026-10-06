"""PostgreSQL connection management, transactions and migrations."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import dict_row

from control.persistence.unit_of_work import UnitOfWork

MIGRATIONS = Path(__file__).with_name("migrations")
T = TypeVar("T")
_RETRYABLE = (pg_errors.SerializationFailure, pg_errors.DeadlockDetected)


class Database:
    """Small thread-safe connection pool; every business write is one transaction."""

    def __init__(self, dsn: str, *, max_connections: int = 16) -> None:
        self.dsn = dsn
        self._idle: queue.LifoQueue[psycopg.Connection] = queue.LifoQueue()
        self._max = max_connections
        self._open = 0
        self._lock = threading.Lock()
        self._available = threading.Semaphore(max_connections)
        self.closed = False

    def _new(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    def _acquire(self) -> psycopg.Connection:
        self._available.acquire()
        try:
            while True:
                try:
                    conn = self._idle.get_nowait()
                except queue.Empty:
                    return self._new()
                if not conn.closed and conn.info.transaction_status == 0:
                    return conn
                conn.close()
        except BaseException:
            self._available.release()
            raise

    def _release(self, conn: psycopg.Connection) -> None:
        try:
            if self.closed or conn.closed or conn.info.transaction_status != 0:
                conn.close()
            else:
                self._idle.put(conn)
        finally:
            self._available.release()

    @contextmanager
    def transaction(self, *, readonly: bool = False) -> Iterator[UnitOfWork]:
        """One atomic unit. Readonly uses one REPEATABLE READ snapshot (RFC 04)."""
        conn = self._acquire()
        uow = UnitOfWork(conn)
        try:
            if readonly:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield uow
            if readonly:
                conn.rollback()
            else:
                conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                conn.close()
            raise
        finally:
            self._release(conn)
        for hook in uow.after_commit:
            hook()

    def run(self, fn: Callable[[UnitOfWork], T], *, retries: int = 4) -> T:
        """Run ``fn`` in a transaction, retrying deadlock/serialization failures."""
        delay = 0.02
        for attempt in range(retries + 1):
            try:
                with self.transaction() as uow:
                    return fn(uow)
            except _RETRYABLE:
                if attempt == retries:
                    raise
                time.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")

    def read(self, fn: Callable[[UnitOfWork], T]) -> T:
        with self.transaction(readonly=True) as uow:
            return fn(uow)

    def migrate(self) -> list[str]:
        applied: list[str] = []
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute("SELECT pg_advisory_lock(7231001)")
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
                )
                done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
                for path in sorted(MIGRATIONS.glob("*.sql")):
                    if path.name in done:
                        continue
                    with conn.transaction():
                        conn.execute(path.read_text())
                        conn.execute(
                            "INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,)
                        )
                    applied.append(path.name)
            finally:
                conn.execute("SELECT pg_advisory_unlock(7231001)")
        return applied

    def close(self) -> None:
        self.closed = True
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                break
