"""Phase 1 durable invariants against real Postgres.

Append-only journal, sequence contiguity, dedupe replay/conflict, claim
generation fencing, worktree barriers, exact-subject target claims, outbox.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def _harness():
    return {
        "provider_id": "opencode",
        "model": "big-pickle",
        "effort": "default",
        "adapter_version": "1",
        "cli_version": "1.18.34",
    }


class TestSessionAuthority:
    def test_create_session(self, pg, workspace):
        from control.application.sessions import SessionService

        svc = SessionService(pg)
        out = svc.create_session(
            principal={"kind": "user", "id": workspace["user_id"]},
            workspace_id=workspace["workspace_id"],
            harness=_harness(),
            effective_input_digest="sha256:in",
            dedupe_key="cs-1",
        )
        assert out["session_id"].startswith("sess_")
        assert out["worktree_id"].startswith("wt_")
        assert out["event_seq"] == 1

        # Dedupe replay returns the stored response without side effects.
        replay = svc.create_session(
            principal={"kind": "user", "id": workspace["user_id"]},
            workspace_id=workspace["workspace_id"],
            harness=_harness(),
            effective_input_digest="sha256:in",
            dedupe_key="cs-1",
        )
        from control.application.commands import Replay

        assert isinstance(replay, Replay)
        assert replay.response["session_id"] == out["session_id"]

    def test_dedupe_conflict(self, pg, workspace):
        from control.application.commands import IdempotencyConflict
        from control.application.sessions import SessionService

        svc = SessionService(pg)
        svc.create_session(
            principal={"kind": "user", "id": workspace["user_id"]},
            workspace_id=workspace["workspace_id"],
            harness=_harness(),
            effective_input_digest="sha256:in",
            dedupe_key="cs-conflict",
        )
        with pytest.raises(IdempotencyConflict):
            svc.create_session(
                principal={"kind": "user", "id": workspace["user_id"]},
                workspace_id=workspace["workspace_id"],
                harness=_harness(),
                effective_input_digest="sha256:CHANGED",
                dedupe_key="cs-conflict",
            )

    def test_queue_turn_enqueues_deduped_dispatch(self, pg, workspace):
        from control.application.sessions import SessionService
        from control.persistence.unit_of_work import SqlUnitOfWork

        svc = SessionService(pg)
        s = svc.create_session(
            principal={"kind": "user", "id": workspace["user_id"]},
            workspace_id=workspace["workspace_id"],
            harness=_harness(),
            effective_input_digest="sha256:in",
        )
        out = svc.post_message(
            principal={"kind": "user", "id": workspace["user_id"]},
            workspace_id=workspace["workspace_id"],
            session_id=s["session_id"],
            author={"kind": "user", "id": workspace["user_id"]},
            routing="queue",
            content={"text": "hello"},
        )
        assert out["turn_id"].startswith("turn_")
        with SqlUnitOfWork(pg) as uow:
            turn = uow.turns.get(workspace["workspace_id"], out["turn_id"])
            assert turn["state"] == "queued"
            events, _ = uow.events.list(workspace["workspace_id"], s["session_id"])
            assert [e["type"] for e in events] == [
                "session.created",
                "message.accepted",
                "turn.queued",
            ]
            job = uow.jobs.get(workspace["workspace_id"], out["dispatch_job"])
            assert job["kind"] == "turn.dispatch"
            assert job["turn_id"] == out["turn_id"]
            uow.rollback()

    def test_journal_append_only(self, pg, workspace):
        import psycopg
        from control.application.sessions import SessionService
        from control.persistence.unit_of_work import SqlUnitOfWork

        svc = SessionService(pg)
        s = svc.create_session(
            principal={"kind": "user", "id": workspace["user_id"]},
            workspace_id=workspace["workspace_id"],
            harness=_harness(),
            effective_input_digest="sha256:in",
        )
        with SqlUnitOfWork(pg) as uow:
            with pytest.raises(psycopg.errors.RaiseException):
                uow.conn.execute(
                    "UPDATE session_events SET payload='{}' WHERE session_id=%s",
                    (s["session_id"],),
                )
            uow.rollback()
        with SqlUnitOfWork(pg) as uow:
            with pytest.raises(psycopg.errors.RaiseException):
                uow.conn.execute(
                    "DELETE FROM session_events WHERE session_id=%s",
                    (s["session_id"],),
                )
            uow.rollback()

    def test_owner_isolation(self, pg):
        from control.application.sessions import SessionService
        from control.domain import ids
        from control.persistence.unit_of_work import SqlUnitOfWork

        # Two owners, two workspaces — identical queries see only their rows.
        wss = []
        with SqlUnitOfWork(pg) as uow:
            for i in range(2):
                u = ids.new_id("user")
                w = ids.new_id("workspace")
                uow.users.insert({"id": u, "email_normalized": f"u{i}@t.c"})
                uow.workspaces.insert({"id": w, "owner_user_id": u, "name": "personal"})
                wss.append((u, w))
            uow.commit()
        svc = SessionService(pg)
        s0 = svc.create_session(
            principal={"kind": "user", "id": wss[0][0]},
            workspace_id=wss[0][1],
            harness=_harness(),
            effective_input_digest="sha256:in",
        )
        with SqlUnitOfWork(pg) as uow:
            # Cross-workspace reads are empty — scope is a WHERE, not a filter.
            assert uow.sessions.get(wss[1][1], s0["session_id"]) is None
            evs, _ = uow.events.list(wss[1][1], s0["session_id"])
            assert evs == []
            uow.rollback()


class TestJobProtocol:
    def _job(self, uow, w):
        from control.application.sessions import enqueue_job
        from control.domain import ids
        from control.domain.jobs import JobKind, TargetFamily

        s = ids.new_id("session")
        uow.sessions.insert(
            {
                "id": s,
                "workspace_id": w,
                "role": "author",
                "harness": {},
                "effective_input_digest": "sha256:x",
            }
        )
        return enqueue_job(
            uow,
            workspace_id=w,
            kind=JobKind.RETENTION_CLEANUP,
            target_family=TargetFamily.SESSION,
            target_id=s,
            dedupe_key=f"ret:{s}",
        )

    def test_claim_and_settle(self, pg, workspace):
        from control.jobs.worker import Worker
        from control.persistence.unit_of_work import SqlUnitOfWork

        w = workspace["workspace_id"]
        with SqlUnitOfWork(pg) as uow:
            self._job(uow, w)
            uow.commit()
        worker = Worker(db=pg, holder="w1")
        claimed = worker.claim()
        assert len(claimed) == 1
        job = claimed[0]
        assert job["claim_holder"] == "w1"
        assert job["claim_generation"] == 1
        # Stale generation cannot settle.
        assert worker.settle({**job, "claim_generation": 0}, outcome="succeeded") is False
        assert worker.settle(job, outcome="succeeded") is True
        with SqlUnitOfWork(pg) as uow:
            assert uow.jobs.get(w, job["id"])["state"] == "succeeded"
            uow.rollback()

    def test_expired_claim_reclaimed(self, pg, workspace):
        from control.jobs.worker import Worker
        from control.persistence.unit_of_work import SqlUnitOfWork

        w = workspace["workspace_id"]
        with SqlUnitOfWork(pg) as uow:
            self._job(uow, w)
            uow.commit()
        worker = Worker(db=pg, holder="w1", lease_seconds=0.0)
        claimed = worker.claim()
        assert claimed
        # claim_expires_at = now() + 0s → immediately reclaimable by another holder.
        worker2 = Worker(db=pg, holder="w2")
        reclaimed = worker2.claim()
        assert len(reclaimed) == 1
        assert reclaimed[0]["claim_generation"] == 2
        # First holder is fenced — its settle is a no-op.
        assert worker.settle({**claimed[0]}, outcome="succeeded") is False

    def test_retry_backoff_and_limit(self, pg, workspace):
        from control.jobs.worker import JobRetry, Worker
        from control.persistence.unit_of_work import SqlUnitOfWork

        w = workspace["workspace_id"]
        with SqlUnitOfWork(pg) as uow:
            job = self._job(uow, w)
            uow.jobs.update(w, job["id"], {"attempt_limit": 1})
            uow.commit()
        worker = Worker(
            db=pg,
            holder="w1",
            handlers={"retention.cleanup": lambda j, c: (_ for _ in ()).throw(JobRetry("flaky"))},
        )
        worker.run_until_idle()
        with SqlUnitOfWork(pg) as uow:
            row = uow.jobs.get(w, job["id"])
            # attempt_limit=1 → first failure is terminal.
            assert row["state"] == "failed"
            assert row["last_error"]["code"] == "attempt_limit"
            uow.rollback()

    def test_active_dedupe_single_flight(self, pg, workspace):
        from control.application.sessions import enqueue_job
        from control.domain.jobs import JobKind, TargetFamily
        from control.persistence.unit_of_work import SqlUnitOfWork

        w = workspace["workspace_id"]
        with SqlUnitOfWork(pg) as uow:
            j1 = self._job(uow, w)
            j2 = enqueue_job(
                uow,
                workspace_id=w,
                kind=JobKind.RETENTION_CLEANUP,
                target_family=TargetFamily.SESSION,
                target_id=j1["session_id"],
                dedupe_key=j1["dedupe_key"],
            )
            assert j2["id"] == j1["id"]
            uow.rollback()


class TestWorktreeBarriers:
    def test_single_active_barrier(self, pg, workspace):
        import psycopg
        from control.application.sessions import SessionService
        from control.domain import ids
        from control.persistence.unit_of_work import SqlUnitOfWork

        svc = SessionService(pg)
        s = svc.create_session(
            principal={"kind": "user", "id": workspace["user_id"]},
            workspace_id=workspace["workspace_id"],
            harness=_harness(),
            effective_input_digest="sha256:in",
        )
        w = workspace["workspace_id"]
        with SqlUnitOfWork(pg) as uow:
            uow.worktree_ops.insert(
                {
                    "id": ids.new_id("worktree_operation"),
                    "workspace_id": w,
                    "worktree_id": s["worktree_id"],
                    "kind": "capture",
                    "operation_id": ids.new_id("operation"),
                    "fence_generation": 1,
                    "state": "active",
                }
            )
            uow.commit()
        with SqlUnitOfWork(pg) as uow:
            with pytest.raises(psycopg.errors.UniqueViolation):
                uow.worktree_ops.insert(
                    {
                        "id": ids.new_id("worktree_operation"),
                        "workspace_id": w,
                        "worktree_id": s["worktree_id"],
                        "kind": "restore",
                        "operation_id": ids.new_id("operation"),
                        "fence_generation": 1,
                        "state": "active",
                    }
                )
            uow.rollback()


class TestRuntimeIngest:
    def test_dedupe_and_offset(self, pg, workspace):
        from control.application.events import ingest_runtime_events
        from control.application.sessions import SessionService
        from control.persistence.unit_of_work import SqlUnitOfWork

        svc = SessionService(pg)
        s = svc.create_session(
            principal={"kind": "user", "id": workspace["user_id"]},
            workspace_id=workspace["workspace_id"],
            harness=_harness(),
            effective_input_digest="sha256:in",
        )
        w, lease = workspace["workspace_id"], "lease_x"
        recs = [
            {"local_seq": 1, "type": "turn.progress", "payload": {"n": 1}},
            {"local_seq": 2, "type": "turn.progress", "payload": {"n": 2}},
        ]
        with SqlUnitOfWork(pg) as uow:
            got = ingest_runtime_events(
                uow,
                workspace_id=w,
                session_id=s["session_id"],
                lease_id=lease,
                runtime_epoch="e1",
                events=recs,
            )
            assert len(got) == 2
            uow.commit()
        # Full replay — no duplicates, offset already covers them.
        with SqlUnitOfWork(pg) as uow:
            got = ingest_runtime_events(
                uow,
                workspace_id=w,
                session_id=s["session_id"],
                lease_id=lease,
                runtime_epoch="e1",
                events=recs,
            )
            assert got == []
            events, _ = uow.events.list(w, s["session_id"])
            types = [e["type"] for e in events]
            assert types.count("turn.progress") == 2
            uow.rollback()


class TestOutbox:
    def test_dedupe_and_drain(self, pg, workspace):
        from control.application.sessions import enqueue_outbox
        from control.jobs.worker import drain_outbox
        from control.persistence.unit_of_work import SqlUnitOfWork

        w = workspace["workspace_id"]
        with SqlUnitOfWork(pg) as uow:
            o1 = enqueue_outbox(
                uow,
                workspace_id=w,
                destination="session:s1",
                kind="notify",
                dedupe_key="k1",
                payload={"x": 1},
            )
            o2 = enqueue_outbox(
                uow,
                workspace_id=w,
                destination="session:s1",
                kind="notify",
                dedupe_key="k1",
                payload={"x": 1},
            )
            assert o1["id"] == o2["id"]
            uow.commit()
        assert drain_outbox(pg) == 1
        assert drain_outbox(pg) == 0


class TestTargetClaims:
    def test_exact_subject_claim_contention(self, pg, workspace):
        from control.domain import ids
        from control.persistence.unit_of_work import SqlUnitOfWork

        w = workspace["workspace_id"]
        c1 = ids.new_id("delivery_target_claim")
        with SqlUnitOfWork(pg) as uow:
            row = uow.target_claims.claim(
                claim_id=c1,
                workspace_id=w,
                holder="dlv_a",
                repository="soren-labs/sbx-e2e-test",
                ref_or_pr="refs/heads/x",
            )
            assert row["holder"] == "dlv_a"
            # A second live holder cannot steal the same target.
            stolen = uow.target_claims.claim(
                claim_id=ids.new_id("delivery_target_claim"),
                workspace_id=w,
                holder="dlv_b",
                repository="soren-labs/sbx-e2e-test",
                ref_or_pr="refs/heads/x",
            )
            assert stolen is None
            uow.rollback()
