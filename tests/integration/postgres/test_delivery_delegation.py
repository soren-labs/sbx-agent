import os

import psycopg
import pytest
from control.application.changes import Changes
from control.application.connections import Connections
from control.application.delegation import Delegations
from control.application.delivery import Deliveries
from control.application.sessions import Sessions
from control.domain.changes import subject_digest, subject_manifest
from control.domain.errors import DomainError
from control.jobs.claims import Claims
from control.jobs.handlers.delivery import DeliveryHandler
from control.security.vault import EnvelopeVault


def setup(database, principal):
    connections = Connections(database, EnvelopeVault({"1": os.urandom(32)}))
    wid = principal.workspace_ids[0]
    cid = connections.create(
        principal, wid, {"kind": "github", "credential": {"token": "REDACTED"}}, "github"
    )["connection_id"]
    sessions = Sessions(database)
    sid = sessions.create(
        principal,
        wid,
        {"github_connection_id": cid, "repository": "https://github.com/soren-labs/sbx-e2e-test"},
        "session",
    )["session_id"]
    with database.transaction() as repo:
        repo.execute("UPDATE jobs SET state='succeeded'")
        wt = repo.one("SELECT * FROM worktrees WHERE session_id=%s", (sid,))
        manifest = subject_manifest(
            {
                "repository": "https://github.com/soren-labs/sbx-e2e-test",
                "base_sha": "base",
                "files": [],
            }
        )
        cs = "cs_ready"
        repo.execute(
            "INSERT INTO changesets(id,workspace_id,session_id,worktree_id,generation,"
            "origin,manifest,subject_digest,state,base_sha) "
            "VALUES(%s,%s,%s,%s,0,'explicit',%s,%s,'ready','base')",
            (cs, wid, sid, wt["id"], {"subject": manifest, "blobs": {}}, subject_digest(manifest)),
        )
    return connections, sessions, sid, cs


def test_delivery_lost_pr_response_adopts_same_effect_and_survives_restart(database, principal):
    connections, sessions, sid, cs = setup(database, principal)
    deliveries = Deliveries(database)
    created = deliveries.request(principal, cs, {}, "deliver")
    assert deliveries.request(principal, cs, {}, "deliver") == created
    claims = Claims(database)
    claim = claims.take("first")

    class Remote:
        pushes = 0
        created = False

        def materialize(self, credential, delivery, changeset):
            assert credential["token"] == "REDACTED"
            return "head", {"tree": "tree"}

        def push(self, credential, delivery, head):
            self.pushes += 1
            return {"head": head, "adopted": self.pushes > 1}

        def pull_request(self, credential, delivery, head):
            if not self.created:
                self.created = True
                raise RuntimeError("response lost after remote PR created")
            return {"pr_number": 1, "pr_url": "https://github.com/soren-labs/sbx-e2e-test/pull/1"}

    remote = Remote()
    handler = DeliveryHandler(database, claims, connections, remote)
    with pytest.raises(RuntimeError):
        handler(claim)
    with database.transaction() as repo:
        repo.execute("UPDATE jobs SET claim_expires_at=now()-interval '1 second'")
    reclaimed = claims.take("after-restart")
    assert reclaimed.row["effect_id"] == claim.row["effect_id"]
    handler(reclaimed)
    claims.finish(reclaimed)
    with database.transaction() as repo:
        row = repo.one("SELECT * FROM deliveries WHERE id=%s", (created["delivery_id"],))
        assert row["state"] == "succeeded" and row["mapped_head"] == row["remote_head"] == "head"
        assert row["pr_number"] == 1
        assert repo.one("SELECT count(*) AS n FROM deliveries")["n"] == 1
    with pytest.raises(DomainError, match="stale_subject"):
        deliveries.merge(
            principal,
            created["delivery_id"],
            {
                "expected_version": row["version"],
                "subject_digest": "wrong",
                "expected_head": "head",
            },
            "merge",
        )
    with pytest.raises(psycopg.Error, match="sealed_immutable"):
        with database.transaction() as repo:
            repo.execute("UPDATE changesets SET subject_digest='tampered' WHERE id=%s", (cs,))


def test_delegation_owns_distinct_worktree_wait_and_no_shipping_authority(database, principal):
    _, sessions, sid, cs = setup(database, principal)
    delegations = Delegations(database, sessions)
    spawned = delegations.spawn(principal, sid, {"changeset_id": cs, "role": "review"}, "spawn")
    assert (
        delegations.spawn(principal, sid, {"changeset_id": cs, "role": "review"}, "spawn")
        == spawned
    )
    waiting = delegations.wait(principal, spawned["delegation_id"], "wait")
    assert waiting["state"] == "pending"
    with database.transaction() as repo:
        trees = repo.all("SELECT * FROM worktrees ORDER BY id")
        assert len(trees) == 2 and trees[0]["id"] != trees[1]["id"]
        child = repo.one("SELECT * FROM sessions WHERE id=%s", (spawned["child_session_id"],))
        assert child["effective_inputs"]["input_changeset_id"] == cs
        assert child["effective_inputs"]["result_contract"]["kind"] == "ReviewAssessment"
    Changes(database, None).capture(
        principal, spawned["child_session_id"], {"generation": 0}, "childcapture"
    )
    # Shipping privilege is a platform gate independent of inherited credentials.
    with database.transaction() as repo:
        child_cs = repo.one(
            "SELECT id FROM changesets WHERE session_id=%s", (spawned["child_session_id"],)
        )["id"]
        repo.execute("UPDATE changesets SET state='ready' WHERE id=%s", (child_cs,))
    with pytest.raises(DomainError, match="forbidden"):
        Deliveries(database).request(principal, child_cs, {}, "ship-child")
    assert delegations.cancel(principal, spawned["delegation_id"], "cancel")["state"] == "cancelled"
    assert sessions.get(principal, spawned["child_session_id"])["turns"][0]["state"] == "cancelled"
