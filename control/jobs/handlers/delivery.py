import hashlib
import io
import zipfile

from control.domain.delivery import merge_reasons
from control.domain.errors import DomainError, require
from control.domain.events import canonical
from control.domain.identity import Principal, new_id


class DeliveryHandler:
    def __init__(self, uow, claims, connections, github):
        self.uow, self.claims, self.connections, self.github = uow, claims, connections, github

    def claim_target(self, claim, delivery):
        with self.uow.transaction() as repo:
            self.claims.assert_current(repo, claim)
            row = repo.one(
                "INSERT INTO delivery_target_claims(repository,target,generation,holde"
                "r,expires_at) "
                "VALUES(%s,%s,1,%s,now()+interval '2 minutes') ON CONFLICT(repository,target) "
                "DO UPDATE SET generation=delivery_target_claims.generation+1,holder=e"
                "xcluded.holder,"
                "expires_at=excluded.expires_at WHERE delivery_target_claims.expires_at<now() "
                "OR delivery_target_claims.holder=excluded.holder RETURNING generation",
                (delivery["repository"], delivery["target_ref"], claim.row["effect_id"]),
            )
            require(row is not None, "waiting_capacity")
            return row["generation"]

    def verify_target(self, repo, claim, delivery, generation):
        self.claims.assert_current(repo, claim)
        row = repo.one(
            "SELECT *,expires_at>now() AS valid FROM delivery_target_claims WHERE repository=%s "
            "AND target=%s FOR UPDATE",
            (delivery["repository"], delivery["target_ref"]),
        )
        require(
            row is not None
            and row["generation"] == generation
            and row["holder"] == claim.row["effect_id"]
            and row["valid"],
            "version_conflict",
        )
        repo.execute(
            "UPDATE delivery_target_claims SET expires_at=now()+interval '2 minutes' "
            "WHERE repository=%s AND target=%s",
            (delivery["repository"], delivery["target_ref"]),
        )

    def credential(self, delivery, effect):
        principal = Principal(delivery["author_id"], (delivery["workspace_id"],))
        return self.connections.resolve(
            principal, delivery["connection_id"], "delivery", operation_id=effect
        )[0]

    def step(self, claim, delivery, generation, kind, evidence, updates=None):
        with self.uow.transaction() as repo:
            repo.one("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (delivery["session_id"],))
            self.verify_target(repo, claim, delivery, generation)
            repo.execute(
                "INSERT INTO delivery_steps(id,workspace_id,delivery_id,kind,effect_id,evidence) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    new_id("step"),
                    delivery["workspace_id"],
                    delivery["id"],
                    kind,
                    claim.row["effect_id"] + ":" + kind,
                    evidence,
                ),
            )
            if updates:
                allowed = {"mapped_head", "remote_head", "pr_number", "pr_url", "state", "reason"}
                require(set(updates) <= allowed, "forbidden")
                assignments = ",".join(k + "=%s" for k in updates)
                repo.execute(
                    "UPDATE deliveries SET " + assignments + ",version=version+1 WHERE id=%s",
                    tuple(updates.values()) + (delivery["id"],),
                )
                delivery.update(updates)
            repo.event(
                delivery["workspace_id"],
                delivery["session_id"],
                "delivery.succeeded"
                if updates and updates.get("state") == "succeeded"
                else "delivery.progressed",
                {"delivery_id": delivery["id"], "step": kind, "evidence": evidence},
            )

    def __call__(self, claim):
        with self.uow.transaction() as repo:
            delivery = repo.one("SELECT * FROM deliveries WHERE id=%s", (claim.row["delivery_id"],))
            changeset = repo.one(
                "SELECT * FROM changesets WHERE id=%s", (delivery["changeset_id"],)
            )
        require(
            changeset["state"] == "ready"
            and delivery["subject_digest"] == changeset["subject_digest"],
            "stale_subject",
        )
        generation = self.claim_target(claim, delivery)
        try:
            if claim.row["kind"] == "delivery.merge":
                return self.merge(claim, delivery, changeset, generation)
            if claim.row["kind"] == "delivery.reconcile":
                remote = self.github.observe(
                    self.credential(delivery, claim.row["effect_id"]), delivery
                )
                require(remote["head"] == delivery["mapped_head"], "remote_head_changed")
                self.step(claim, delivery, generation, "reconcile", remote)
                return
            if delivery["state"] == "succeeded":
                return
            if delivery["transport"] == "export":
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("subject.json", canonical(changeset["manifest"]["subject"]))
                    for path, key in changeset["manifest"]["blobs"].items():
                        archive.writestr(
                            "files/" + path, self.github.objects.get(delivery["workspace_id"], key)
                        )
                content = output.getvalue()
                key = self.github.objects.put(delivery["workspace_id"], content)
                blob_id = new_id("blob")
                with self.uow.transaction() as repo:
                    self.verify_target(repo, claim, delivery, generation)
                    repo.execute(
                        "INSERT INTO blobs(id,workspace_id,storage_key,digest,size,class,state) "
                        "VALUES(%s,%s,%s,%s,%s,'export','ready')",
                        (
                            blob_id,
                            delivery["workspace_id"],
                            key,
                            hashlib.sha256(content).hexdigest(),
                            len(content),
                        ),
                    )
                self.step(
                    claim,
                    delivery,
                    generation,
                    "export",
                    {"blob_id": blob_id},
                    {"state": "succeeded"},
                )
                return
            head = delivery["mapped_head"]
            if not head:
                head, evidence = self.github.materialize(
                    self.credential(delivery, claim.row["effect_id"]), delivery, changeset
                )
                self.step(
                    claim,
                    delivery,
                    generation,
                    "materialize",
                    evidence,
                    {"mapped_head": head, "state": "executing"},
                )
            pushed = self.github.push(
                self.credential(delivery, claim.row["effect_id"]), delivery, head
            )
            self.step(claim, delivery, generation, "push", pushed, {"remote_head": head})
            if delivery["transport"] == "pull_request":
                pr = self.github.pull_request(
                    self.credential(delivery, claim.row["effect_id"]), delivery, head
                )
                self.step(
                    claim,
                    delivery,
                    generation,
                    "pull_request",
                    pr,
                    {"pr_number": pr["pr_number"], "pr_url": pr["pr_url"]},
                )
            self.step(
                claim,
                delivery,
                generation,
                "verified",
                {"head": head, "subject_digest": delivery["subject_digest"]},
                {"state": "succeeded"},
            )
        except DomainError as error:
            if error.code != "version_conflict" and claim.row["kind"] != "delivery.merge":
                self.step(
                    claim,
                    delivery,
                    generation,
                    "blocked",
                    {"reason": error.code},
                    {"state": "blocked", "reason": error.code},
                )
            raise
        finally:
            with self.uow.transaction() as repo:
                # Releasing a target hint does not authorize another worker commit.
                repo.execute(
                    "UPDATE delivery_target_claims SET expires_at=now() WHERE reposito"
                    "ry=%s AND target=%s "
                    "AND holder=%s AND generation=%s",
                    (
                        delivery["repository"],
                        delivery["target_ref"],
                        claim.row["effect_id"],
                        generation,
                    ),
                )

    def result_rows(self, repo, delivery, changeset):
        rows = repo.all(
            "SELECT r.*,d.parent_session_id,d.child_session_id AS assigned_child FROM "
            "delegation_results r "
            "JOIN delegations d ON d.id=r.delegation_id WHERE d.changeset_id=%s",
            (changeset["id"],),
        )
        for row in rows:
            child = repo.one(
                "SELECT id FROM worktrees WHERE session_id=%s", (row["child_session_id"],)
            )
            parent = repo.one(
                "SELECT id FROM worktrees WHERE session_id=%s", (row["parent_session_id"],)
            )
            row["independent"] = (
                row["assigned_child"] != row["parent_session_id"] and child["id"] != parent["id"]
            )
        return rows

    def merge(self, claim, delivery, changeset, generation):
        with self.uow.transaction() as repo:
            request = repo.one(
                "SELECT * FROM merge_requests WHERE id=%s", (claim.row["effect_id"],)
            )
            results = self.result_rows(repo, delivery, changeset)
        credential = self.credential(delivery, claim.row["effect_id"])
        remote = self.github.observe(credential, delivery)
        if remote.get("merged"):
            # A lost merge response can be reconciled only for the pinned exact head.
            require(
                remote["head"] == request["expected_head"] and remote.get("merge_commit_sha"),
                "delivery_unresolved",
            )
            evidence = {"merged": True, "sha": remote["merge_commit_sha"], "adopted": True}
        else:
            reasons = merge_reasons(delivery, changeset, results, remote)
            if reasons:
                with self.uow.transaction() as repo:
                    self.verify_target(repo, claim, delivery, generation)
                    repo.execute(
                        "UPDATE merge_requests SET state='blocked',evidence=%s WHERE id=%s",
                        ({"reasons": reasons}, request["id"]),
                    )
                raise DomainError(reasons[0])
            # No stale Console badge can pass this immediate remote exact-head check.
            require(
                request["subject_digest"] == delivery["subject_digest"]
                and request["expected_head"] == remote["head"],
                "stale_subject",
            )
            evidence = self.github.merge(
                self.credential(delivery, claim.row["effect_id"]), delivery, request
            )
            verified = self.github.observe(
                self.credential(delivery, claim.row["effect_id"]), delivery
            )
            require(
                verified.get("merged") and verified.get("merge_commit_sha") == evidence["sha"],
                "delivery_unresolved",
            )
        with self.uow.transaction() as repo:
            repo.one("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (delivery["session_id"],))
            self.verify_target(repo, claim, delivery, generation)
            repo.execute(
                "UPDATE merge_requests SET state='succeeded',evidence=%s WHERE id=%s",
                (evidence, request["id"]),
            )
            repo.event(
                delivery["workspace_id"],
                delivery["session_id"],
                "delivery.merged",
                {"delivery_id": delivery["id"], "evidence": evidence},
            )
