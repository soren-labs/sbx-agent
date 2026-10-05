"""Typed row-mapping helpers shared by repositories.

Repositories are explicit SQL — no ORM. Rows map to dicts; the application
layer constructs domain objects. All statements are parameterized.
"""

from __future__ import annotations

import enum as _enum
from collections.abc import Iterable
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


class Rows:
    """Bound-connection helper. ``conn`` must have dict_row row factory."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def one(self, sql: str, params: Iterable[Any] | dict = ()) -> dict | None:
        return self.conn.execute(sql, _params(params)).fetchone()

    def all(self, sql: str, params: Iterable[Any] | dict = ()) -> list[dict]:
        return list(self.conn.execute(sql, _params(params)))

    def one_required(self, sql: str, params: Iterable[Any] | dict = ()) -> dict:
        row = self.one(sql, params)
        if row is None:
            from control.domain.errors import NotFound

            raise NotFound("row")
        return row

    def insert_row(self, table: str, row: dict[str, Any]) -> dict:
        cols = list(row)
        placeholders = ", ".join(f"%({c})s" for c in cols)
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) RETURNING *"
        return self.conn.execute(sql, _adapted(row)).fetchone()

    def update_row(
        self,
        table: str,
        key: dict[str, Any],
        changes: dict[str, Any],
        *,
        expected_version: int | None = None,
        version_column: str | None = "version",
    ) -> dict | None:
        """Conditional update. ``expected_version`` adds CAS + increments."""
        sets = []
        params: dict[str, Any] = {}
        for i, (col, val) in enumerate(changes.items()):
            if isinstance(val, _SqlExpr):
                sets.append(f"{col} = {val}")
                continue
            p = f"c{i}"
            sets.append(f"{col} = %({p})s")
            params[p] = _adapt(val)
        where = []
        for i, (col, val) in enumerate(key.items()):
            p = f"k{i}"
            where.append(f"{col} = %({p})s")
            params[p] = val
        if expected_version is not None and version_column:
            where.append(f"{version_column} = %(ev)s")
            params["ev"] = expected_version
            sets.append(f"{version_column} = {version_column} + 1")
        sql = f"UPDATE {table} SET {', '.join(sets)} WHERE {' AND '.join(where)} RETURNING *"
        cur = self.conn.execute(sql, params)
        return cur.fetchone()


def _params(params):
    """Named-param dicts pass through; positional sequences become tuples."""
    if isinstance(params, dict):
        return params
    return tuple(params)


def _adapt(value: Any) -> Any:
    """Map a Python value to a psycopg parameter for a typed column.

    dict/list land in jsonb columns: wrap in ``Jsonb`` so the parameter is
    typed jsonb, not text. (A bare ``= ANY(%s)`` list is passed raw by
    callers, not through here — psycopg adapts it to an array.)
    """
    if isinstance(value, (dict, list)):
        return Jsonb(value)
    if isinstance(value, _enum.Enum):
        return value.value
    return value


def _adapted(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _adapt(v) for k, v in row.items()}


def _now():
    """Server-side timestamp marker — raw SQL executed in update sets."""
    return _SqlExpr("now()")


def sqlexpr(sql: str):
    """Inline SQL expression marker for update set-clauses."""
    return _SqlExpr(sql)


class _SqlExpr(str):
    """Trusted inline SQL emitted in a SET clause (never user data)."""

    __slots__ = ()
