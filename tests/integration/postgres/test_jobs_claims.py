"""Durable Job claims, fencing and dedupe on real PostgreSQL (A04)."""

from __future__ import annotations

import threading
import time

import psycopg
import pytest
from control.domain.errors import DomainError
from control.jobs import claims
from control.jobs.model import Continue, Succeeded
from control.jobs.worker import Worker
from tests.support.factories import make_principal, session_body, sessions_app


def _session(db):
    principal = make_principal(db)
    sid = sessions_app(db).create(principal, principal.default_workspace_id, session_body())[
        "session_id"
    ]
    return principal, sid


def _enqueue(db, workspace_id, sid, n):
    def fn(uow):
        return [
            uow.enqueue_job(
                workspace_id=workspace_id,
                kind="retention.cleanup",
                target_id=sid,
                dedupe_key=f"{sid}:{i}",
            )
            for i in range(n)
        ]

    return db.run(fn)


def test_skip_locked_claims_each_job_once(db) -> None:
    principal, sid = _session(db)
    ids = _enqueue(db, principal.default_workspace_id, sid, 40)
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(n):
        while True:
            c = claims.claim_next(db, f"w{n}", kinds=["retention.cleanup"])
            if c is None:
                return
            with lock:
                claimed.append(c.job_id)
            db.run(lambda u, c=c: claims.finish(u, c, Succeeded()))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(ids)


def test_stale_worker_completion_rejected_and_successor_sees_same_effect(db) -> None:
    principal, sid = _session(db)
    [job_id] = _enqueue(db, principal.default_workspace_id, sid, 1)
    first = claims.claim_next(db, "w1", lease_seconds=0.2)
    assert first.generation == 1
    time.sleep(0.35)
    second = claims.claim_next(db, "w2", lease_seconds=30)
    assert second.job_id == job_id and second.generation == 2 and second.reclaimed
    assert second.effect_id == first.effect_id
    with pytest.raises(claims.StaleClaim):
        db.run(lambda u: claims.finish(u, first, Succeeded({"by": "w1"})))
    db.run(lambda u: claims.finish(u, second, Succeeded({"by": "w2"})))
    job = db.read(lambda u: u.get("jobs", job_id))
    assert job["state"] == "succeeded" and job["result"] == {"by": "w2"}
    attempts = db.read(lambda u: u.find("job_attempts", {"job_id": job_id}, order="generation"))
    assert [a["outcome"] for a in attempts] == ["claim_expired", "succeeded"]


def test_active_dedupe_and_typed_target_constraint(db) -> None:
    principal, sid = _session(db)
    wsp = principal.default_workspace_id
    a = db.run(lambda u: u.enqueue_job(workspace_id=wsp, kind="retention.cleanup", target_id=sid))
    b = db.run(lambda u: u.enqueue_job(workspace_id=wsp, kind="retention.cleanup", target_id=sid))
    assert a == b
    c = claims.claim_next(db, "w")
    db.run(lambda u: claims.finish(u, c, Succeeded()))
    assert (
        db.run(lambda u: u.enqueue_job(workspace_id=wsp, kind="retention.cleanup", target_id=sid))
        != a
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        db.run(
            lambda u: u.insert(
                "jobs",
                {
                    "id": "job_x",
                    "workspace_id": wsp,
                    "kind": "x",
                    "target_family": "turn",
                    "session_id": sid,
                    "dedupe_key": "x",
                    "effect_id": "x",
                },
            )
        )
    with pytest.raises(ValueError):
        db.run(
            lambda u: u.enqueue_job(
                workspace_id=wsp,
                kind="retention.cleanup",
                target_id=sid,
                dedupe_key="s",
                input={"token": "x"},
            )
        )


def test_worker_outcomes_retry_fail_continue(db) -> None:
    principal, sid = _session(db)
    wsp = principal.default_workspace_id
    calls = {"n": 0}

    def handler(ctx):
        calls["n"] += 1
        mode = ctx.input.get("mode")
        if mode == "retry":
            raise DomainError("executor_unavailable", "transient")
        if mode == "fail":
            raise DomainError("validation_failed", "permanent")
        if mode == "continue" and calls["n"] < 3:
            return Continue(delay=0)
        return Succeeded()

    db.run(
        lambda u: [
            u.enqueue_job(
                workspace_id=wsp,
                kind="retention.cleanup",
                target_id=sid,
                dedupe_key=m,
                input={"mode": m},
            )
            for m in ("retry", "fail", "continue")
        ]
    )
    Worker(db, {"retention.cleanup": handler}).run_until_idle()
    jobs = {
        j["dedupe_key"]: j for j in db.read(lambda u: u.find("jobs", {"kind": "retention.cleanup"}))
    }
    assert (
        jobs["retry"]["state"] == "retry_wait"
        and jobs["retry"]["last_error_code"] == "executor_unavailable"
    )
    assert jobs["fail"]["state"] == "failed"
    assert jobs["continue"]["state"] == "succeeded"


def test_commit_under_claim_is_atomic_with_completion(db) -> None:
    principal, sid = _session(db)
    wsp = principal.default_workspace_id
    db.run(lambda u: u.enqueue_job(workspace_id=wsp, kind="retention.cleanup", target_id=sid))

    def handler(ctx):
        ctx.commit(
            lambda u: u.insert("login_attempts", {"email": "audit@x", "succeeded": True}),
            Succeeded({"ok": 1}),
        )

    Worker(db, {"retention.cleanup": handler}).run_until_idle()
    assert db.read(lambda u: u.count("login_attempts", {"email": "audit@x"})) == 1
    assert (
        db.read(lambda u: u.find_one("jobs", {"kind": "retention.cleanup"}))["state"] == "succeeded"
    )


def test_keepalive_holds_the_claim_through_a_slow_call_and_stops_after_it(db) -> None:
    """#198: a live worker in a slow valid allocation is not reclaimed; a dead one is."""
    principal, sid = _session(db)
    _enqueue(db, principal.default_workspace_id, sid, 1)
    seen: dict[str, object] = {}

    def slow(ctx):
        with ctx.keepalive():
            time.sleep(1.0)  # several claim leases
            seen["rival"] = claims.claim_next(db, "rival", kinds=["retention.cleanup"])
        ctx.commit(lambda uow: None)  # still the current claim
        return Succeeded({"ok": True})

    worker = Worker(db, {"retention.cleanup": slow}, lease_seconds=0.3)
    assert worker.run_once() is True
    assert seen["rival"] is None, "no successor while the holder is alive and renewing"
    job = db.read(lambda uow: uow.find_one("jobs", {"session_id": sid}))
    assert job["state"] == "succeeded" and job["claim_generation"] == 1


def test_keepalive_does_not_outlive_its_bound_or_mask_a_lost_claim(db) -> None:
    principal, sid = _session(db)
    _enqueue(db, principal.default_workspace_id, sid, 1)
    seen: dict[str, object] = {}

    def hung(ctx):
        with ctx.keepalive(max_seconds=0.2):
            time.sleep(1.0)  # the bounded heartbeat gives up; the claim expires
            seen["rival"] = claims.claim_next(db, "rival", kinds=["retention.cleanup"])
        with pytest.raises(claims.StaleClaim):
            ctx.commit(lambda uow: None)
        raise claims.StaleClaim(ctx.claim, "superseded")

    worker = Worker(db, {"retention.cleanup": hung}, lease_seconds=0.3)
    assert worker.run_once() is True
    assert seen["rival"] is not None and seen["rival"].generation == 2
