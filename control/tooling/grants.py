import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from control.domain.errors import require
from control.domain.identity import Principal, new_id
from control.security.identity import hashed
from control.tooling.gateway import SessionTools


class ToolGrants:
    def __init__(self, resources, master, origin):
        self.resources, self.master, self.origin = resources, master, origin

    def issue(self, session, execution, lease):
        if not self.origin:
            return None
        value = hmac.new(
            self.master, ("tool-v1:" + execution["id"]).encode(), hashlib.sha256
        ).hexdigest()
        with self.resources.uow.transaction() as repo:
            epoch = repo.one(
                "SELECT identity_version FROM users WHERE id=%s", (session["creator_id"],)
            )["identity_version"]
        actions = (
            ["read", "result", "publish_result"]
            if session["role"] not in {"coding", "developer", "coordinator"}
            else ["read", "spawn", "message", "wait", "result", "cancel", "apply"]
        )
        with self.resources.uow.transaction() as repo:
            repo.execute(
                "INSERT INTO tool_grants(id,workspace_id,session_id,turn_id,execution_"
                "id,lease_id,lease_generation,token_hash,actions,expires_at,auth_epoch) VALUES(%s"
                ",%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(execution_id) DO NOTHING",
                (
                    new_id("tool"),
                    session["workspace_id"],
                    session["id"],
                    execution["turn_id"],
                    execution["id"],
                    lease["id"],
                    lease["generation"],
                    hashed(value),
                    actions,
                    datetime.now(UTC) + timedelta(seconds=600),
                    epoch,
                ),
            )
        return {"url": self.origin.rstrip("/") + "/internal/tools", "token": value}

    def invoke(self, value, name, arguments, key):
        with self.resources.uow.transaction() as repo:
            grant = repo.one(
                "SELECT g.*,s.creator_id FROM tool_grants g JOIN sessions s ON s.id=g."
                "session_id JOIN users u ON u.id=s.creator_id "
                "JOIN executor_leases l ON l.id=g.lease_id JOIN executions "
                "e ON e.id=g.execution_id WHERE g.token_hash=%s AND g.expires_at>now()"
                " AND g.revoked_at IS NULL AND g.auth_epoch=u.identity_version AND l.s"
                "tate='ready' AND l.generation=g.leas"
                "e_generation AND e.state IN ('preparing','started','stop_requested') "
                "AND s.lifecycle='open'",
                (hashed(value),),
            )
            require(grant is not None and name in grant["actions"], "forbidden")
        principal = Principal(grant["creator_id"], (grant["workspace_id"],))
        return SessionTools(self.resources, principal, grant["session_id"]).invoke(
            name, arguments, "tool:" + grant["execution_id"] + ":" + key
        )
