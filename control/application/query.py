"""Bounded, owner-scoped projections. No effect settlement or network calls."""

from control.application.access import owned, workspace
from control.domain.errors import require

TABLES = {
    "projects",
    "project_versions",
    "sessions",
    "connections",
    "changesets",
    "deliveries",
    "delegations",
    "snapshots",
    "service_desires",
    "jobs",
}
SAFE_JOB = ("id", "kind", "state", "attempts", "last_error", "created_at", "due_at")


class Queries:
    def __init__(self, uow):
        self.uow = uow

    def list(self, principal, wid, table, *, limit=50, cursor=None, filters=None):
        workspace(principal, wid)
        require(table in TABLES and 1 <= limit <= 100, "invalid_cursor")
        require(cursor is None or len(cursor) < 100, "invalid_cursor")
        clauses, params = ["workspace_id=%s"], [wid]
        for column, value in (filters or {}).items():
            require(column in {"session_id", "project_id", "lifecycle", "role"}, "invalid_cursor")
            clauses.append(column + "=%s")
            params.append(value)
        if cursor:
            clauses.append("id>%s")
            params.append(cursor)
        with self.uow.transaction() as repo:
            rows = repo.all(
                "SELECT * FROM "
                + table
                + " WHERE "
                + " AND ".join(clauses)
                + " ORDER BY id LIMIT %s",
                tuple(params) + (limit + 1,),
            )
        more = len(rows) > limit
        rows = rows[:limit]
        if table == "connections":
            rows = [{k: v for k, v in r.items() if k != "current_credential_id"} for r in rows]
        if table == "jobs":
            rows = [{k: r[k] for k in SAFE_JOB} for r in rows]
        return {"items": rows, "next_cursor": rows[-1]["id"] if more else None}

    def events(self, principal, sid, after=0, limit=100):
        require(after >= 0 and 1 <= limit <= 200, "invalid_cursor")
        with self.uow.transaction() as repo:
            repo.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            session = owned(repo, "sessions", sid, principal)
            watermark = session["next_event_seq"] - 1
            require(after <= watermark, "invalid_cursor")
            events = repo.all(
                "SELECT * FROM session_events WHERE session_id=%s AND seq>%s ORDER BY seq LIMIT %s",
                (sid, after, limit),
            )
            return {
                "events": events,
                "event_watermark": watermark,
                "next_cursor": events[-1]["seq"] if events else after,
            }

    def detail(self, principal, table, rid):
        require(table in TABLES | {"turns"}, "not_found")
        with self.uow.transaction() as repo:
            row = owned(repo, table, rid, principal)
            if table == "jobs":
                return {k: row[k] for k in SAFE_JOB}
            if table == "deliveries":
                row["steps"] = repo.all(
                    "SELECT kind,evidence,created_at FROM delivery_steps "
                    "WHERE delivery_id=%s ORDER BY created_at,id",
                    (rid,),
                )
                row["merge_requests"] = repo.all(
                    "SELECT id,state,evidence FROM merge_requests "
                    "WHERE delivery_id=%s ORDER BY created_at,id",
                    (rid,),
                )
            return row
