from datetime import UTC, datetime, timedelta

from control.application.access import owned
from control.domain.errors import require
from control.domain.identity import new_id
from control.security.identity import hashed, token


class Services:
    def __init__(self, uow, io, preview_origin=None):
        self.uow, self.io, self.preview_origin = uow, io, preview_origin

    def list(self, principal, sid):
        with self.uow.transaction() as repo:
            owned(repo, "sessions", sid, principal)
            rows = repo.all(
                "SELECT * FROM service_desires WHERE session_id=%s ORDER BY name", (sid,)
            )
            for row in rows:
                row["instance"] = repo.one(
                    "SELECT id,state,port,lease_id FROM service_instances WHERE sessio"
                    "n_id=%s AND name=%s ORDER BY observed_at DESC NULLS LAST LIMIT 1",
                    (sid, row["name"]),
                )
        return {"items": rows}

    def action(self, principal, service_id, action, key):
        with self.uow.transaction() as repo:
            service = owned(repo, "service_desires", service_id, principal)
        declaration = service["declaration"]
        return self.io.operation(
            principal,
            service["session_id"],
            "service.start" if action == "start" else "service.stop",
            {
                "service_id": service_id,
                "command": declaration["argv"],
                "port": declaration.get("port"),
            },
            key,
        )

    def preview(self, principal, service_id):
        require(self.preview_origin is not None, "unsupported_capability")
        with self.uow.transaction() as repo:
            service = owned(repo, "service_desires", service_id, principal)
            lease = repo.one(
                "SELECT * FROM executor_leases WHERE session_id=%s AND state='ready'",
                (service["session_id"],),
            )
            require(lease is not None, "executor_unavailable")
            instance = repo.one(
                "SELECT id FROM service_instances WHERE session_id=%s AND name=%s AND "
                "lease_id=%s AND state='running'",
                (service["session_id"], service["name"], lease["id"]),
            )
            require(
                instance is not None and service["declaration"].get("port"), "executor_unavailable"
            )
            grant = token()
            repo.execute(
                "INSERT INTO preview_grants(id,workspace_id,service_id,lease_id,token_"
                "hash,expires_at) VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    new_id("preview"),
                    service["workspace_id"],
                    service_id,
                    lease["id"],
                    hashed(grant),
                    datetime.now(UTC) + timedelta(minutes=5),
                ),
            )
        return {"url": self.preview_origin + "/p/" + grant + "/", "expires_in": 300}
