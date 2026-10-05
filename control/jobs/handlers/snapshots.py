import json

from control.domain.errors import require
from control.domain.events import canonical, digest
from control.runtime_client.grants import runtime_token


class SnapshotHandler:
    def __init__(self, uow, claims, executor_factory, objects, master):
        self.uow, self.claims, self.executor_factory = uow, claims, executor_factory
        self.objects, self.master = objects, master

    def __call__(self, claim):
        with self.uow.transaction() as repo:
            snap = repo.one("SELECT * FROM snapshots WHERE id=%s", (claim.row["snapshot_id"],))
            wt = repo.one("SELECT * FROM worktrees WHERE id=%s", (snap["worktree_id"],))
            session = repo.one("SELECT * FROM sessions WHERE id=%s", (wt["session_id"],))
            lease = repo.one(
                "SELECT * FROM executor_leases WHERE session_id=%s AND state='ready'",
                (session["id"],),
            )
            require(lease is not None, "executor_unavailable")
            require(
                not repo.one(
                    "SELECT id FROM turns WHERE session_id=%s AND state IN ('preparing"
                    "','running','cancelling')",
                    (session["id"],),
                ),
                "waiting_capacity",
            )
        token = runtime_token(self.master, lease["id"], lease["generation"])
        client = self.executor_factory(session, lease, token).connect_runtime(lease["handle"])
        client.submit(claim.row["effect_id"], "snapshot.capture", {})
        result = client.wait(claim.row["effect_id"])
        require("manifest" in result, result.get("error", "capture_failed"))
        require(digest(result["manifest"]) == result["content_digest"], "capture_failed")
        content = canonical(result["manifest"]).encode()
        key = self.objects.put(session["workspace_id"], content)
        with self.uow.transaction() as repo:
            repo.one("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (session["id"],))
            self.claims.assert_current(repo, claim)
            current = repo.one("SELECT * FROM worktrees WHERE id=%s FOR UPDATE", (wt["id"],))
            require(
                current["generation"] == snap["generation"] == result["manifest"]["generation"],
                "version_conflict",
            )
            safe_manifest = {
                "object_key": key,
                "content_digest": result["content_digest"],
                "cli_version": result["manifest"]["cli_version"],
                "session_id": session["id"],
            }
            repo.execute(
                "UPDATE snapshots SET state='ready',manifest=%s,content_digest=%s,even"
                "t_watermark=%s WHERE id=%s",
                (
                    safe_manifest,
                    result["content_digest"],
                    session["next_event_seq"] - 1,
                    snap["id"],
                ),
            )
            repo.execute(
                "UPDATE worktrees SET last_snapshot_id=%s,recovery_watermark=%s WHERE id=%s",
                (snap["id"], session["next_event_seq"] - 1, wt["id"]),
            )
            repo.execute(
                "UPDATE worktree_operations SET state='succeeded' WHERE id=%s", (snap["id"],)
            )
            repo.event(
                session["workspace_id"],
                session["id"],
                "snapshot.ready",
                {"snapshot_id": snap["id"]},
            )

    def read(self, workspace, snapshot_id):
        with self.uow.transaction() as repo:
            snap = repo.one(
                "SELECT * FROM snapshots WHERE id=%s AND workspace_id=%s AND state='ready'",
                (snapshot_id, workspace),
            )
            require(snap is not None, "context_unavailable")
        manifest = json.loads(self.objects.get(workspace, snap["manifest"]["object_key"]))
        require(digest(manifest) == snap["content_digest"], "capture_failed")
        return manifest


class ReleaseHandler:
    def __init__(self, uow, claims, executor_factory, master):
        self.uow, self.claims, self.executor_factory, self.master = (
            uow,
            claims,
            executor_factory,
            master,
        )

    def __call__(self, claim):
        with self.uow.transaction() as repo:
            lease = repo.one("SELECT * FROM executor_leases WHERE id=%s", (claim.row["lease_id"],))
            session = repo.one("SELECT * FROM sessions WHERE id=%s", (lease["session_id"],))
        if lease["cleanup_confirmed"]:
            return
        token = runtime_token(self.master, lease["id"], lease["generation"])
        backend = self.executor_factory(session, lease, token)
        handle = lease["handle"] or backend.lookup(lease["allocation_operation_id"])
        require(handle is not None, "outcome_unknown")
        if handle:
            backend.terminate(handle, claim.row["effect_id"])
            require(backend.describe(handle)["status"] == "stopped", "outcome_unknown")
        with self.uow.transaction() as repo:
            repo.one("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (session["id"],))
            self.claims.assert_current(repo, claim)
            repo.execute(
                "UPDATE executor_leases SET cleanup_confirmed=true,state=CASE WHEN state='lost' "
                "THEN 'lost' ELSE 'released' END WHERE id=%s",
                (lease["id"],),
            )
            repo.execute(
                "UPDATE capacity_reservations SET state='released' WHERE lease_id=%s",
                (lease["id"],),
            )
            repo.execute(
                "UPDATE worktrees SET availability=CASE WHEN last_snapshot_id IS NULL "
                "THEN 'unavailable' "
                "ELSE 'checkpointed' END WHERE session_id=%s",
                (session["id"],),
            )
            repo.event(
                session["workspace_id"],
                session["id"],
                "executor.released",
                {"lease_id": lease["id"]},
            )
