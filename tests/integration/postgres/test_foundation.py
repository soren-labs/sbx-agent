from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from control.application.projections import replay
from control.application.sessions import Sessions
from control.domain.errors import DomainError
from control.domain.identity import Principal, new_id
from control.jobs.claims import Claims
from control.jobs.fences import advance, verify
from control.persistence.database import Database


def setup_session(database, principal):
    service = Sessions(database)
    sid = service.create(principal, principal.workspace_ids[0], {}, "create")["session_id"]
    return service, sid


def test_intent_replay_restart_and_conflict(database, principal):
    service, sid = setup_session(database, principal)
    first = service.send(principal, sid, {"content": "hello"}, "one")
    restarted = Sessions(Database(database.dsn))
    assert restarted.send(principal, sid, {"content": "hello"}, "one") == first
    with pytest.raises(DomainError, match="idempotency_conflict"):
        restarted.send(principal, sid, {"content": "different"}, "one")
    with database.transaction() as repo:
        assert repo.one("SELECT count(*) AS n FROM jobs")["n"] == 1
        assert repo.one("SELECT count(*) AS n FROM messages")["n"] == 1


def test_concurrent_queue_and_claims(database, principal):
    service, sid = setup_session(database, principal)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(
                lambda i: service.send(principal, sid, {"content": f"message {i}"}, f"key{i}"),
                range(16),
            )
        )
    assert len({r["turn_id"] for r in responses}) == 16
    with database.transaction() as repo:
        assert [
            r["ordinal"] for r in repo.all("SELECT ordinal FROM turns ORDER BY ordinal")
        ] == list(range(1, 17))
        seqs = [r["seq"] for r in repo.all("SELECT seq FROM session_events ORDER BY seq")]
        assert seqs == list(range(1, 34))
    claims = Claims(database)
    with ThreadPoolExecutor(max_workers=8) as pool:
        taken = list(pool.map(lambda i: claims.take(str(i)), range(16)))
    # SKIP LOCKED may see all remaining rows locked at a particular instant.
    # A worker retries its poll; this is not permission to claim an occupied row.
    taken = [claim for claim in taken if claim is not None]
    while remaining := claims.take("drain"):
        taken.append(remaining)
    assert len(taken) == len({c.job_id for c in taken}) == 16


def test_stale_claim_retains_effect_identity(database, principal):
    service, sid = setup_session(database, principal)
    service.send(principal, sid, {"content": "hello"}, "one")
    claims = Claims(database)
    old = claims.take("old")
    with database.transaction() as repo:
        repo.execute("UPDATE jobs SET claim_expires_at=now()-interval '1 second'")
    current = claims.take("new")
    assert current.generation == old.generation + 1
    assert current.row["effect_id"] == old.row["effect_id"]
    with pytest.raises(DomainError, match="version_conflict"):
        claims.finish(old)
    claims.finish(current)


def test_rollback_journal_constraints_and_owner(database, principal):
    service, sid = setup_session(database, principal)
    before = service.get(principal, sid)["event_watermark"]
    with pytest.raises(RuntimeError):
        with database.transaction() as repo:
            repo.event(principal.workspace_ids[0], sid, "session.settings_changed", {})
            raise RuntimeError("fault after event and outbox")
    assert service.get(principal, sid)["event_watermark"] == before
    with pytest.raises(psycopg.Error, match="immutable_record"):
        with database.transaction() as repo:
            repo.execute("UPDATE session_events SET type='wrong'")
    with pytest.raises(DomainError, match="not_found"):
        service.get(Principal(new_id("usr"), ()), sid)
    turn = service.send(principal, sid, {"content": "hello"}, "one")["turn_id"]
    service.cancel(principal, turn, "cancel")
    with pytest.raises(psycopg.Error, match="terminal_immutable"):
        with database.transaction() as repo:
            repo.execute("UPDATE turns SET state='succeeded' WHERE id=%s", (turn,))


def test_reads_have_no_effects(database, principal):
    service, sid = setup_session(database, principal)
    service.send(principal, sid, {"content": "hello"}, "one")
    with database.transaction() as repo:
        before = {
            t: repo.all(f"SELECT * FROM {t} ORDER BY id")
            for t in ("sessions", "turns", "jobs", "session_events", "outbox_messages")
        }
    for _ in range(4):
        service.get(principal, sid)
    with database.transaction() as repo:
        after = {t: repo.all(f"SELECT * FROM {t} ORDER BY id") for t in before}
    assert before == after


def test_rebuild_and_resource_fence(database, principal):
    service, sid = setup_session(database, principal)
    tid = service.send(principal, sid, {"content": "work"}, "queue")["turn_id"]
    service.cancel(principal, tid, "cancel")
    with database.transaction() as repo:
        rebuilt = replay(repo.all("SELECT * FROM session_events ORDER BY seq"))
        assert rebuilt["turns"][tid] == "cancelled"
        assert rebuilt["event_watermark"] == service.get(principal, sid)["event_watermark"]
        gen = advance(repo, principal.workspace_ids[0], sid, "first")
    with database.transaction() as repo:
        repo.execute("UPDATE resource_fences SET expires_at=now()-interval '1 second'")
        assert advance(repo, principal.workspace_ids[0], sid, "second") == gen + 1
    with pytest.raises(DomainError, match="version_conflict"):
        with database.transaction() as repo:
            verify(repo, principal.workspace_ids[0], sid, "first", gen)
