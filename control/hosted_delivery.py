"""Owner-App revision delivery shared by requests and durable automatic intent."""

from control.revisions import RevisionService


def owner_revision_service(base, github):
    service = RevisionService(
        base._store,
        base._artifacts,
        workspaces=base._workspaces,
        env={},
        remote=github.remote,
        env_for_repo=github.git_env,
        push_payload_fn=github.push_payload if github.mock else None,
        ls_remote_fn=github.ls_remote if github.mock else None,
        refresh_delivery=True,
    )
    service._lock = base._lock
    return service


class HostedAutoDelivery:
    def __init__(self, plane, tasks, github, revisions):
        self.plane, self.tasks, self.github, self.revisions = plane, tasks, github, revisions

    def deliver(self, agent_id):
        agent = self.plane.store.get(agent_id)
        task = self.tasks.find_by_agent(agent_id)
        workspace = self.plane.workspaces.get(agent_id)
        if (
            agent is None
            or agent.status == "closed"
            or task is None
            or task.owner != agent.owner
            or workspace is None
            or not (workspace.git or {}).get("auto_publish")
        ):
            return
        revision = self.revisions.latest(agent_id)
        if revision is None or revision.status != "ready" or revision.task_id != task.id:
            return
        # Only a durable successful run authorizes delivery. This also protects
        # the materialize-before-verdict crash/cancel window on reconstruction.
        if not revision.run_id or not revision.run_id.startswith("run-"):
            return
        run = self.plane.run_ledger.get(agent_id, int(revision.run_id[4:]))
        if run is None or run.status != "FINISHED":
            return
        service = owner_revision_service(self.revisions, self.github.for_user(agent.owner))
        try:
            service.deliver(revision, automatic=True)
        except Exception:
            with service._lock:
                current = service.get(revision.revision_id)
                if (current.delivery or {}).get("status") not in {"failed", "delivered"}:
                    service._record_delivery_failure(
                        current,
                        workspace,
                        workspace.branch,
                        "repo_unavailable",
                        "owner GitHub App delivery unavailable",
                    )

    def reconcile(self):
        # Workspace auto_publish + ready artifact + FINISHED ledger are the
        # durable intent. No live sandbox or in-memory finish callback needed.
        for agent in self.plane.store.list_all():
            try:
                self.deliver(agent.id)
            except Exception:
                continue
