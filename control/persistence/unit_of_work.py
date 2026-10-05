"""Unit of Work: typed-table helpers, journal append, Job/outbox enqueue, dedupe.

All SQL lives in the persistence package. Application code receives a
``UnitOfWork`` through the transaction port and never builds SQL itself.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

import psycopg
from protocol.events import SCHEMA_VERSION, is_known
from psycopg import sql
from psycopg.types.json import Jsonb

from control.domain.digests import digest_of
from control.domain.errors import DomainError
from control.domain.ids import new_id
from control.domain.jobs import ACTIVE_JOB_STATES, JOB_KINDS, PRIORITY, TARGET_COLUMNS
from control.persistence import queries
from control.security.redaction import find_secret_fields

TABLES = frozenset(
    {
        "users",
        "password_credentials",
        "email_verifications",
        "login_attempts",
        "workspaces",
        "workspace_memberships",
        "login_sessions",
        "api_keys",
        "projects",
        "project_versions",
        "connections",
        "credential_versions",
        "connection_observations",
        "credential_grants",
        "sessions",
        "messages",
        "message_parts",
        "turns",
        "session_events",
        "executor_leases",
        "executions",
        "native_context_bindings",
        "runtime_ingestion_offsets",
        "resource_fences",
        "capacity_reservations",
        "worktrees",
        "worktree_operations",
        "snapshots",
        "blobs",
        "blob_references",
        "jobs",
        "job_attempts",
        "outbox_messages",
        "command_deduplication",
        "audit_records",
        # Later migrations (changes / delivery / delegation / services).
        "changesets",
        "changeset_files",
        "deliveries",
        "delivery_steps",
        "delivery_target_claims",
        "merge_requests",
        "delegations",
        "delegation_inputs",
        "delegation_results",
        "wait_subscriptions",
        "service_desires",
        "service_instances",
        "tool_grants",
    }
)

DEDUPE_TTL = timedelta(days=7)


def _adapt(value: Any) -> Any:
    if isinstance(value, dict) or (
        isinstance(value, list) and value and isinstance(value[0], dict)
    ):
        return Jsonb(value)
    return value


def _table(name: str) -> sql.Identifier:
    if name not in TABLES:
        raise ValueError(f"unknown table {name}")
    return sql.Identifier(name)


def _where(where: dict[str, Any]) -> tuple[sql.Composable, list[Any]]:
    parts: list[sql.Composable] = []
    params: list[Any] = []
    for column, value in where.items():
        ident = sql.Identifier(column)
        if value is None:
            parts.append(sql.SQL("{} IS NULL").format(ident))
        elif isinstance(value, list | tuple | set | frozenset):
            parts.append(sql.SQL("{} = ANY(%s)").format(ident))
            params.append(list(value))
        else:
            parts.append(sql.SQL("{} = %s").format(ident))
            params.append(value)
    if not parts:
        return sql.SQL("TRUE"), params
    return sql.SQL(" AND ").join(parts), params


class UnitOfWork:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self.after_commit: list[Callable[[], None]] = []
        self.events: list[dict[str, Any]] = []
        self._now: datetime | None = None

    # -- raw access (persistence-internal) ------------------------------------------
    def _one(self, query: Any, params: Any = None) -> dict[str, Any] | None:
        return self.conn.execute(query, params).fetchone()

    def _all(self, query: Any, params: Any = None) -> list[dict[str, Any]]:
        return self.conn.execute(query, params).fetchall()

    def now(self) -> datetime:
        if self._now is None:
            self._now = self._one("SELECT now() AS now")["now"]
        return self._now

    def query(self, name: str, **params: Any) -> list[dict[str, Any]]:
        """Named specialized query from ``control.persistence.queries``."""
        text = queries.NAMED[name]
        return self._all(text, {k: _adapt(v) for k, v in params.items()})

    def query_one(self, name: str, **params: Any) -> dict[str, Any] | None:
        rows = self.query(name, **params)
        return rows[0] if rows else None

    # -- typed table helpers ---------------------------------------------------------
    def insert(self, table: str, values: dict[str, Any]) -> dict[str, Any]:
        cols = list(values)
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
            _table(table),
            sql.SQL(", ").join(map(sql.Identifier, cols)),
            sql.SQL(", ").join(sql.Placeholder() * len(cols)),
        )
        return self._one(query, [_adapt(values[c]) for c in cols])

    def get(
        self,
        table: str,
        id: str,
        *,
        workspace_ids: Iterable[str] | None = None,
        lock: bool = False,
    ) -> dict[str, Any] | None:
        where: dict[str, Any] = {"id": id}
        if workspace_ids is not None:
            where["workspace_id"] = list(workspace_ids)
        return self.find_one(table, where, lock=lock)

    def find(
        self,
        table: str,
        where: dict[str, Any] | None = None,
        *,
        order: str | None = None,
        limit: int | None = None,
        lock: bool = False,
    ) -> list[dict[str, Any]]:
        clause, params = _where(where or {})
        query = sql.SQL("SELECT * FROM {} WHERE {}").format(_table(table), clause)
        if order:
            terms = []
            for term in order.split(","):
                column, _, direction = term.strip().partition(" ")
                direction = direction.strip().upper() or "ASC"
                if direction not in ("ASC", "DESC"):
                    raise ValueError("bad order")
                terms.append(sql.SQL("{} " + direction).format(sql.Identifier(column)))
            query += sql.SQL(" ORDER BY ") + sql.SQL(", ").join(terms)
        if limit is not None:
            query += sql.SQL(" LIMIT {}").format(sql.Literal(int(limit)))
        if lock:
            query += sql.SQL(" FOR UPDATE")
        return self._all(query, params)

    def find_one(
        self, table: str, where: dict[str, Any], *, lock: bool = False, order: str | None = None
    ) -> dict[str, Any] | None:
        rows = self.find(table, where, lock=lock, limit=1, order=order)
        return rows[0] if rows else None

    def count(self, table: str, where: dict[str, Any] | None = None) -> int:
        clause, params = _where(where or {})
        query = sql.SQL("SELECT count(*) AS n FROM {} WHERE {}").format(_table(table), clause)
        return int(self._one(query, params)["n"])

    def update(
        self,
        table: str,
        id: str,
        values: dict[str, Any],
        *,
        expect: dict[str, Any] | None = None,
        bump_version: bool = False,
    ) -> dict[str, Any] | None:
        """CAS update: returns the new row or ``None`` when ``expect`` did not match."""
        sets = [sql.SQL("{} = %s").format(sql.Identifier(c)) for c in values]
        params = [_adapt(v) for v in values.values()]
        if bump_version:
            sets.append(sql.SQL("version = version + 1"))
        clause, where_params = _where({"id": id, **(expect or {})})
        query = sql.SQL("UPDATE {} SET {} WHERE {} RETURNING *").format(
            _table(table), sql.SQL(", ").join(sets), clause
        )
        return self._one(query, params + where_params)

    def update_where(self, table: str, where: dict[str, Any], values: dict[str, Any]) -> int:
        sets = [sql.SQL("{} = %s").format(sql.Identifier(c)) for c in values]
        params = [_adapt(v) for v in values.values()]
        clause, where_params = _where(where)
        query = sql.SQL("UPDATE {} SET {} WHERE {}").format(
            _table(table), sql.SQL(", ").join(sets), clause
        )
        return self.conn.execute(query, params + where_params).rowcount

    # -- Session ordinal allocation and journal ----------------------------------------
    def allocate(self, session_id: str, counter: str) -> int:
        if counter not in ("next_event_seq", "next_message_ordinal", "next_turn_ordinal"):
            raise ValueError(counter)
        query = sql.SQL(
            "UPDATE sessions SET {c} = {c} + 1 WHERE id = %s RETURNING {c} - 1 AS value"
        ).format(c=sql.Identifier(counter))
        row = self._one(query, (session_id,))
        if row is None:
            raise DomainError("not_found", "session not found")
        return int(row["value"])

    def append_event(
        self,
        session: dict[str, Any] | str,
        type: str,
        payload: dict[str, Any] | None = None,
        *,
        actor: str,
        source: str = "application",
        workspace_id: str | None = None,
        observed_at: datetime | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        turn_id: str | None = None,
        execution_id: str | None = None,
        executor_lease_id: str | None = None,
        lease_generation: int | None = None,
        delegation_id: str | None = None,
        changeset_id: str | None = None,
        delivery_id: str | None = None,
        runtime_epoch: str | None = None,
        local_seq: int | None = None,
    ) -> dict[str, Any]:
        if not is_known(type):
            raise ValueError(f"unknown event type {type}")
        payload = payload or {}
        leaked = find_secret_fields(payload)
        if leaked:
            raise ValueError(f"secret-looking payload fields rejected: {leaked}")
        session_id = session if isinstance(session, str) else session["id"]
        if workspace_id is None:
            workspace_id = session["workspace_id"] if isinstance(session, dict) else None
        seq = self.allocate(session_id, "next_event_seq")
        if workspace_id is None:
            workspace_id = self._one(
                "SELECT workspace_id FROM sessions WHERE id = %s", (session_id,)
            )["workspace_id"]
        row = self.insert(
            "session_events",
            {
                "id": new_id("event"),
                "workspace_id": workspace_id,
                "session_id": session_id,
                "seq": seq,
                "type": type,
                "schema_version": SCHEMA_VERSION,
                "observed_at": observed_at,
                "actor": actor,
                "source": source,
                "causation_id": causation_id,
                "correlation_id": correlation_id,
                "turn_id": turn_id,
                "execution_id": execution_id,
                "executor_lease_id": executor_lease_id,
                "lease_generation": lease_generation,
                "delegation_id": delegation_id,
                "changeset_id": changeset_id,
                "delivery_id": delivery_id,
                "runtime_epoch": runtime_epoch,
                "local_seq": local_seq,
                "payload": Jsonb(payload),
            },
        )
        self.conn.execute("SELECT pg_notify('sbx_events', %s)", (session_id,))
        self.events.append(row)
        return row

    # -- Jobs and outbox --------------------------------------------------------------
    def enqueue_job(
        self,
        *,
        workspace_id: str,
        kind: str,
        target_id: str,
        dedupe_key: str | None = None,
        effect_id: str | None = None,
        input: dict[str, Any] | None = None,
        priority: int | None = None,
        due_at: datetime | None = None,
        deadline_at: datetime | None = None,
        max_attempts: int = 20,
        session_id: str | None = None,
    ) -> str:
        """Insert a deduped durable Job; returns the existing active Job id on conflict."""
        family = JOB_KINDS[kind]
        column = TARGET_COLUMNS[family]
        if input and find_secret_fields(input):
            raise ValueError("credentials must never be serialized into Job inputs")
        dedupe_key = dedupe_key or target_id
        values: dict[str, Any] = {
            "id": new_id("job"),
            "workspace_id": workspace_id,
            "kind": kind,
            "target_family": family,
            column: target_id,
            "dedupe_key": dedupe_key,
            "effect_id": effect_id or f"{kind}:{dedupe_key}",
            "input": Jsonb(input or {}),
            "priority": priority if priority is not None else _default_priority(kind),
            "max_attempts": max_attempts,
        }
        if session_id and column != "session_id":
            values["session_id"] = session_id
        if due_at is not None:
            values["due_at"] = due_at
        if deadline_at is not None:
            values["deadline_at"] = deadline_at
        cols = list(values)
        query = sql.SQL(
            "INSERT INTO jobs ({}) VALUES ({}) ON CONFLICT (kind, dedupe_key) "
            "WHERE state IN ('queued', 'claimed', 'retry_wait') DO NOTHING RETURNING id"
        ).format(
            sql.SQL(", ").join(map(sql.Identifier, cols)),
            sql.SQL(", ").join(sql.Placeholder() * len(cols)),
        )
        row = self._one(query, [values[c] for c in cols])
        if row is None:
            row = self.find_one(
                "jobs", {"kind": kind, "dedupe_key": dedupe_key, "state": list(ACTIVE_JOB_STATES)}
            )
            if row is None:  # pragma: no cover - concurrent completion between statements
                raise DomainError("version_conflict", "job dedupe race; retry")
        self.conn.execute("SELECT pg_notify('sbx_jobs', %s)", (kind,))
        return row["id"]

    def outbox(
        self,
        *,
        workspace_id: str,
        destination: str,
        dedupe_key: str,
        payload: dict[str, Any],
        session_id: str | None = None,
        event_seq: int | None = None,
    ) -> str | None:
        row = self._one(
            "INSERT INTO outbox_messages (id, workspace_id, destination, dedupe_key, session_id,"
            " event_seq, payload) VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
            (
                new_id("outbox"),
                workspace_id,
                destination,
                dedupe_key,
                session_id,
                event_seq,
                Jsonb(payload),
            ),
        )
        return row["id"] if row else None

    def audit(
        self,
        *,
        actor: str,
        action: str,
        target_kind: str,
        target_id: str,
        result: str,
        workspace_id: str | None = None,
        purpose: str | None = None,
        target_version: int | None = None,
    ) -> None:
        self.insert(
            "audit_records",
            {
                "id": new_id("audit"),
                "workspace_id": workspace_id,
                "actor": actor,
                "action": action,
                "purpose": purpose,
                "target_kind": target_kind,
                "target_id": target_id,
                "target_version": target_version,
                "result": result,
            },
        )

    # -- Command deduplication ------------------------------------------------------------
    def dedupe_begin(
        self,
        *,
        principal_id: str,
        workspace_id: str,
        command_kind: str,
        key: str | None,
        request: Any,
    ) -> dict[str, Any] | None:
        """Return a stored response for a replay, ``None`` when this call owns the key."""
        if not key:
            return None
        digest = digest_of(request)
        claimed = self._one(
            "INSERT INTO command_deduplication (principal_id, workspace_id, command_kind, key,"
            " request_digest, expires_at) VALUES (%s, %s, %s, %s, %s, now() + %s)"
            " ON CONFLICT DO NOTHING RETURNING key",
            (principal_id, workspace_id, command_kind, key, digest, DEDUPE_TTL),
        )
        if claimed:
            return None
        row = self._one(
            "SELECT request_digest, response FROM command_deduplication WHERE principal_id = %s"
            " AND workspace_id = %s AND command_kind = %s AND key = %s",
            (principal_id, workspace_id, command_kind, key),
        )
        if row["request_digest"] != digest:
            raise DomainError(
                "idempotency_conflict",
                "Idempotency-Key was reused with a different request body",
                details={"command": command_kind},
            )
        return row["response"] or {}

    def dedupe_finish(
        self,
        *,
        principal_id: str,
        workspace_id: str,
        command_kind: str,
        key: str | None,
        response: dict[str, Any],
    ) -> None:
        if not key:
            return
        self.conn.execute(
            "UPDATE command_deduplication SET response = %s WHERE principal_id = %s"
            " AND workspace_id = %s AND command_kind = %s AND key = %s",
            (
                Jsonb(json.loads(json.dumps(response, default=str))),
                principal_id,
                workspace_id,
                command_kind,
                key,
            ),
        )

    def watermark(self, session_id: str) -> int:
        row = self._one("SELECT next_event_seq - 1 AS w FROM sessions WHERE id = %s", (session_id,))
        return int(row["w"]) if row else 0


def _default_priority(kind: str) -> int:
    if kind in ("executor.release", "retention.cleanup"):
        return PRIORITY["cleanup"]
    if kind in ("delegation.cancel",):
        return PRIORITY["cancel"]
    if kind.endswith(".reconcile"):
        return PRIORITY["reconcile"]
    if kind in ("turn.dispatch",):
        return PRIORITY["dispatch"]
    if kind in ("connection.validate",):
        return PRIORITY["validate"]
    return PRIORITY["effect"]
