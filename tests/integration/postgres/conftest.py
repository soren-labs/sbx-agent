"""Postgres integration fixtures — disposable local test database only.

Runs against SBX_TEST_DATABASE_URL (never a production DSN). Each test gets
a fully migrated schema truncated clean. Skips without the DSN.
"""

from __future__ import annotations

import os

import pytest

TEST_DSN = os.environ.get("SBX_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def pg_dsn():
    if not TEST_DSN:
        pytest.skip("SBX_TEST_DATABASE_URL not set — disposable PG required")
    return TEST_DSN


@pytest.fixture()
def pg(pg_dsn):
    """Migrated, per-test-truncated Database."""
    from control.persistence.database import Database

    db = Database(pg_dsn)
    db.migrate()
    conn = db.acquire()
    try:
        tables = [
            r["table_name"]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema='public' AND table_name <> 'schema_migrations'"
            )
        ]
        if tables:
            conn.execute("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE")
        conn.commit()
    finally:
        db.release(conn)
    yield db
    db.close()


@pytest.fixture()
def uow(pg):
    from control.persistence.unit_of_work import SqlUnitOfWork

    return SqlUnitOfWork


@pytest.fixture()
def workspace(pg):
    """A user + personal workspace pair for scoped-row tests."""
    from control.domain import ids
    from control.persistence.unit_of_work import SqlUnitOfWork

    user_id, ws_id = ids.new_id("user"), ids.new_id("workspace")
    with SqlUnitOfWork(pg) as uow:
        uow.users.insert({"id": user_id, "email_normalized": "test@example.com"})
        uow.workspaces.insert({"id": ws_id, "owner_user_id": user_id, "name": "personal"})
        uow.commit()
    return {"user_id": user_id, "workspace_id": ws_id}
