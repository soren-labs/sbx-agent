from control.application.access import owned
from control.application.deduplication import command
from control.domain.delivery import DEFAULT_POLICY, validate_transport
from control.domain.errors import require
from control.domain.identity import new_id
from control.domain.projects import repository_identity


class Deliveries:
    def __init__(self, uow):
        self.uow = uow

    def request(self, principal, csid, body, key):
        with self.uow.transaction() as repo:
            cs = owned(repo, "changesets", csid, principal)
            session = owned(repo, "sessions", cs["session_id"], principal, lock=True)

            def perform():
                require(cs["state"] == "ready", "capture_failed")
                require(
                    not repo.one(
                        "SELECT id FROM delegations WHERE child_session_id=%s", (session["id"],)
                    ),
                    "forbidden",
                )
                transport = body.get("transport", "pull_request")
                validate_transport(transport)
                cid = None
                repository = cs["manifest"]["subject"].get("repository")
                if transport != "export":
                    cid = body.get("github_connection_id") or session["github_connection_id"]
                    connection = owned(repo, "connections", cid, principal)
                    require(
                        connection["kind"] == "github" and connection["state"] == "configured",
                        "connection_revoked",
                    )
                    repository = repository_identity(body.get("repository") or repository)
                    require(repository == cs["manifest"]["subject"]["repository"], "stale_subject")
                else:
                    repository = repository or "workspace:" + cs["workspace_id"]
                policy = {**DEFAULT_POLICY, **session["effective_inputs"].get("ship_policy", {})}
                if body.get("automatic"):
                    require(cs["automatic_eligible"] and policy["automatic"], "forbidden")
                did = new_id("dlv")
                target = "sbx/" + session["id"] + "/" + csid
                repo.execute(
                    "INSERT INTO deliveries(id,workspace_id,session_id,changeset_id,subject_digest,"
                    "repository,target_ref,base_ref,expected_head,transport,policy,aut"
                    "hor_id,connection_id) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        did,
                        cs["workspace_id"],
                        session["id"],
                        csid,
                        cs["subject_digest"],
                        repository,
                        target,
                        body.get("base_ref", "main"),
                        body.get("expected_head"),
                        transport,
                        policy,
                        principal.user_id,
                        cid,
                    ),
                )
                job = repo.enqueue(cs["workspace_id"], "delivery.perform", did, did + "-perform")
                seq = repo.event(
                    cs["workspace_id"],
                    session["id"],
                    "delivery.requested",
                    {"delivery_id": did, "subject_digest": cs["subject_digest"]},
                )
                return {"delivery_id": did, "job_id": job, "event_watermark": seq}

            return command(
                repo,
                principal,
                cs["workspace_id"],
                "delivery.request",
                key,
                {"changeset_id": csid, **body},
                perform,
            )

    def merge(self, principal, did, body, key):
        with self.uow.transaction() as repo:
            delivery = owned(repo, "deliveries", did, principal, lock=True)

            def perform():
                require(
                    delivery["state"] == "succeeded"
                    and delivery["version"] == body["expected_version"],
                    "version_conflict",
                )
                require(
                    delivery["subject_digest"] == body["subject_digest"]
                    and delivery["mapped_head"] == body["expected_head"],
                    "stale_subject",
                )
                require(
                    body.get("method", "squash") in delivery["policy"]["merge_methods"], "forbidden"
                )
                mid = new_id("merge")
                repo.execute(
                    "INSERT INTO merge_requests(id,workspace_id,delivery_id,expected_v"
                    "ersion,subject_digest,"
                    "expected_head,expected_base,method) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        mid,
                        delivery["workspace_id"],
                        did,
                        delivery["version"],
                        delivery["subject_digest"],
                        body["expected_head"],
                        body.get("expected_base"),
                        body.get("method", "squash"),
                    ),
                )
                job = repo.enqueue(delivery["workspace_id"], "delivery.merge", did, mid)
                repo.event(
                    delivery["workspace_id"],
                    delivery["session_id"],
                    "delivery.merge_requested",
                    {
                        "delivery_id": did,
                        "merge_request_id": mid,
                        "subject_digest": delivery["subject_digest"],
                    },
                )
                return {"merge_request_id": mid, "job_id": job}

            return command(
                repo,
                principal,
                delivery["workspace_id"],
                "delivery.merge",
                key,
                {"delivery_id": did, **body},
                perform,
            )

    def retry(self, principal, did, key, *, reconcile=False):
        with self.uow.transaction() as repo:
            row = owned(repo, "deliveries", did, principal, lock=True)

            def perform():
                kind = "delivery.reconcile" if reconcile else "delivery.perform"
                require(
                    reconcile or row["state"] in {"failed", "blocked", "pending"},
                    "version_conflict",
                )
                job = repo.one("SELECT id FROM jobs WHERE kind=%s AND delivery_id=%s", (kind, did))
                if job:
                    repo.execute(
                        "UPDATE jobs SET state='queued',due_at=now(),deadline=now()+in"
                        "terval '24 hours' "
                        "WHERE id=%s AND state!='claimed'",
                        (job["id"],),
                    )
                    job_id = job["id"]
                else:
                    job_id = repo.enqueue(
                        row["workspace_id"], kind, did, did + "-" + kind.split(".")[1]
                    )
                return {"delivery_id": did, "job_id": job_id}

            return command(
                repo,
                principal,
                row["workspace_id"],
                "delivery.retry",
                key,
                {"delivery_id": did, "reconcile": reconcile},
                perform,
            )
