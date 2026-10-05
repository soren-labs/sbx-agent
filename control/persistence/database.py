from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from control.persistence.unit_of_work import SQLRepository


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn

    @contextmanager
    def transaction(self):
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.transaction():
                yield SQLRepository(connection)

    def migrate(self):
        with self.transaction() as repo:
            repo.execute("SELECT pg_advisory_xact_lock(167167)")
            repo.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version text PRIMARY KEY, applied_at timestamptz DEFAULT now())"
            )
            for path in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
                if not repo.one(
                    "SELECT version FROM schema_migrations WHERE version=%s", (path.name,)
                ):
                    repo.execute(path.read_text())
                    repo.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (path.name,))
