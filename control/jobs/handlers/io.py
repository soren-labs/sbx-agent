import base64

from control.domain.errors import require
from control.domain.identity import Principal


class IOHandler:
    def __init__(self, uow, claims, io, objects):
        self.uow, self.claims, self.io, self.objects = uow, claims, io, objects

    def __call__(self, claim):
        close = claim.row["kind"] == "worktree.terminal_close"
        oid = claim.row["effect_id"].removesuffix("-close") if close else claim.row["effect_id"]
        with self.uow.transaction() as repo:
            op = repo.one("SELECT * FROM worktree_operations WHERE id=%s", (oid,))
            if op["state"] in {"succeeded", "failed"}:
                return
            wt = repo.one("SELECT * FROM worktrees WHERE id=%s", (op["worktree_id"],))
            session = repo.one("SELECT * FROM sessions WHERE id=%s", (wt["session_id"],))
        principal = Principal(session["creator_id"], (session["workspace_id"],))
        client = self.io.client(principal, session["id"])
        require(client.hello["lease_id"] == op["lease_id"], "executor_unavailable")
        payload, kind = dict(op["payload"]), op["kind"]
        if close:
            kind, payload = "terminal.close", {"terminal_id": oid}
        if kind == "terminal.open":
            payload["terminal_id"] = oid
        if kind == "changes.apply":
            with self.uow.transaction() as repo:
                cs = repo.one(
                    "SELECT * FROM changesets WHERE id=%s", (payload.pop("changeset_id"),)
                )
            payload.update(
                subject=cs["manifest"]["subject"],
                contents={
                    p: base64.b64encode(self.objects.get(session["workspace_id"], key)).decode()
                    for p, key in cs["manifest"]["blobs"].items()
                },
            )
        client.submit(claim.row["effect_id"], kind, payload)
        result = client.wait(claim.row["effect_id"])
        with self.uow.transaction() as repo:
            repo.one("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (session["id"],))
            self.claims.assert_current(repo, claim)
            state = (
                "failed"
                if result.get("error")
                else ("executing" if kind == "terminal.open" else "succeeded")
            )
            repo.execute(
                "UPDATE worktree_operations SET state=%s,result=%s WHERE id=%s",
                (state, result, oid),
            )
            if kind in {"service.start", "service.stop"}:
                service = repo.one(
                    "SELECT * FROM service_desires WHERE id=%s", (payload["service_id"],)
                )
                desired = "running" if kind == "service.start" else "stopped"
                repo.execute(
                    "UPDATE service_desires SET desired_state=%s WHERE id=%s",
                    (desired, service["id"]),
                )
                repo.execute(
                    "INSERT INTO service_instances(id,workspace_id,session_id,name,lease_id,"
                    "lease_generation,state,port,observed_at) VALUES(%s,%s,%s,%s,%s,%s"
                    ",%s,%s,now()) "
                    "ON CONFLICT(session_id,name,lease_id) DO UPDATE SET state=excluded.state,"
                    "observed_at=excluded.observed_at",
                    (
                        claim.row["effect_id"],
                        session["workspace_id"],
                        session["id"],
                        service["name"],
                        op["lease_id"],
                        client.hello["lease_generation"],
                        desired if not result.get("error") else "failed",
                        payload.get("port"),
                    ),
                )
            if result.get("generation") is not None:
                repo.execute(
                    "UPDATE worktrees SET generation=%s WHERE id=%s AND generation=%s",
                    (result["generation"], wt["id"], op["expected_generation"]),
                )
            repo.event(
                session["workspace_id"],
                session["id"],
                "worktree.operation_completed",
                {"operation_id": oid, "state": state},
            )
