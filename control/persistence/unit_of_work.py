from psycopg.types.json import Jsonb

from control.domain.errors import require
from control.domain.identity import new_id

JOB_TARGETS = {
    "turn": "turn_id",
    "execution": "execution_id",
    "executor": "lease_id",
    "snapshot": "snapshot_id",
    "changeset": "changeset_id",
    "delivery": "delivery_id",
    "delegation": "delegation_id",
    "connection": "connection_id",
    "service": "service_id",
    "worktree": "worktree_id",
}


class SQLRepository:
    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def _args(args):
        return tuple(Jsonb(x) if isinstance(x, dict) else x for x in args)

    def one(self, sql, args=()):
        return self.connection.execute(sql, self._args(args)).fetchone()

    def all(self, sql, args=()):
        return self.connection.execute(sql, self._args(args)).fetchall()

    def execute(self, sql, args=()):
        self.connection.execute(sql, self._args(args))

    def event(self, workspace, session, kind, payload, **refs):
        row = self.one(
            "UPDATE sessions SET next_event_seq=next_event_seq+1,version=version+1,"
            "updated_at=now() WHERE workspace_id=%s AND id=%s RETURNING next_event_seq-1 AS seq",
            (workspace, session),
        )
        require(row is not None, "not_found")
        event = new_id("evt")
        self.execute(
            "INSERT INTO session_events(id,workspace_id,session_id,seq,type,payload,source,"
            "turn_id,execution_id,executor_lease_id,runtime_epoch,local_seq) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                event,
                workspace,
                session,
                row["seq"],
                kind,
                Jsonb(payload),
                Jsonb(refs.get("source", {"kind": "application"})),
                refs.get("turn_id"),
                refs.get("execution_id"),
                refs.get("executor_lease_id"),
                refs.get("runtime_epoch"),
                refs.get("local_seq"),
            ),
        )
        self.execute(
            "INSERT INTO outbox_messages(id,workspace_id,session_id,event_id,dedupe_key) "
            "VALUES (%s,%s,%s,%s,%s)",
            (new_id("out"), workspace, session, event, event),
        )
        return row["seq"]

    def enqueue(self, workspace, kind, target, identity):
        family = kind.split(".")[0]
        require(family in JOB_TARGETS, "unsupported_capability")
        column = JOB_TARGETS[family]
        row = self.one(
            f"INSERT INTO jobs(id,workspace_id,kind,target_family,{column},effect_id,dedupe_key) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (dedupe_key) DO UPDATE "
            "SET dedupe_key=excluded.dedupe_key RETURNING id",
            (new_id("job"), workspace, kind, family, target, identity, f"{kind}:{identity}"),
        )
        return row["id"]
