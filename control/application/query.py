"""Bounded, owner-scoped projections. No effect settlement or network calls."""

from control.application.access import owned, workspace
from control.application.assessments import current_policy, result_rows
from control.domain.delivery import merge_reasons
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

    def diagnostics(self, principal, wid):
        """A bounded coherent operational snapshot, without handles or secrets."""
        workspace(principal, wid)
        with self.uow.transaction() as repo:
            repo.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return {
                "observed_at": repo.one("SELECT now() AS observed_at")["observed_at"],
                "jobs": repo.all(
                    "SELECT id,kind,state,effect_id,claim_generation,holder,claim_expires_at,"
                    "attempts,last_error,due_at,deadline FROM jobs WHERE workspace_id=%s "
                    "ORDER BY created_at DESC,id LIMIT 100",
                    (wid,),
                ),
                "fences": repo.all(
                    "SELECT resource,generation,holder,expires_at FROM resource_fences "
                    "WHERE workspace_id=%s ORDER BY resource LIMIT 100",
                    (wid,),
                ),
                "leases": repo.all(
                    "SELECT id,session_id,backend,generation,state,cleanup_confirmed,expires_at "
                    "FROM executor_leases WHERE workspace_id=%s "
                    "ORDER BY created_at DESC,id LIMIT 100",
                    (wid,),
                ),
                "outbox": repo.all(
                    "SELECT id,session_id,event_id,state,claim_generation,holder,claim_expires_at,"
                    "attempts,last_error FROM outbox_messages WHERE workspace_id=%s "
                    "ORDER BY created_at DESC,id LIMIT 100",
                    (wid,),
                ),
                "capacity": repo.all(
                    "SELECT id,connection_id,slot_ordinal,execution_id,lease_id,state,expires_at "
                    "FROM capacity_reservations WHERE workspace_id=%s ORDER BY id LIMIT 100",
                    (wid,),
                ),
            }

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
                changeset = repo.one("SELECT * FROM changesets WHERE id=%s", (row["changeset_id"],))
                observed = next(
                    (step for step in reversed(row["steps"]) if step["kind"] == "reconcile"), None
                )
                effective = {**row, "policy": current_policy(repo, row)}
                reasons = (
                    merge_reasons(
                        effective, changeset, result_rows(repo, changeset), observed["evidence"]
                    )
                    if observed
                    else ["remote_observation_required"]
                )
                row["merge_eligibility"] = {
                    "eligible": not reasons,
                    "reasons": reasons,
                    "observed_at": observed["created_at"] if observed else None,
                    "authority": "advisory",
                }
            return row
