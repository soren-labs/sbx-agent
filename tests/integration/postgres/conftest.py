import os
from uuid import uuid4

import psycopg
import pytest
from control.domain.identity import Principal, new_id
from control.persistence.database import Database
from psycopg.conninfo import make_conninfo


@pytest.fixture
def database():
    dsn = os.environ.get("SBX_TEST_PG_DSN")
    if not dsn:
        pytest.skip("SBX_TEST_PG_DSN must name a disposable PostgreSQL database")
    schema = "test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    db = Database(make_conninfo(dsn, options=f"-c search_path={schema}"))
    db.migrate()
    yield db
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def principal(database):
    uid, wid = new_id("usr"), new_id("wsp")
    with database.transaction() as repo:
        repo.execute("INSERT INTO users(id,email) VALUES(%s,%s)", (uid, uid + "@example.test"))
        repo.execute("INSERT INTO workspaces(id,owner_id,name) VALUES(%s,%s,'Test')", (wid, uid))
    return Principal(uid, (wid,))
