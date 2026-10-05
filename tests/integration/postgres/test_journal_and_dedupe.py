"""Committed journal, command dedupe and transactional authority on real PostgreSQL."""

from __future__ import annotations

import threading

import psycopg
import pytest
from control.application.projections import Queries
from control.domain.errors import DomainError
from tests.support.factories import make_principal, session_body, sessions_app


def _seqs(db, session_id):
    return db.read(
        lambda u: [
            r["seq"] for r in u.find("session_events", {"session_id": session_id}, order="seq")
        ]
    )


def test_migrations_are_idempotent(db) -> None:
    assert db.migrate() == []


def test_sequence_contiguous_and_rollback_consumes_none(db) -> None:
    principal = make_principal(db)
    app = sessions_app(db)
    sid = app.create(principal, principal.default_workspace_id, session_body())["session_id"]
    with pytest.raises(RuntimeError):
        with db.transaction() as uow:
            session = uow.get("sessions", sid, lock=True)
            uow.append_event(session, "session.settings_changed", {"title": "x"}, actor="t")
            raise RuntimeError("abort")
    app.send(principal, sid, {"content": "hello"})
    seqs = _seqs(db, sid)
    assert seqs == list(range(1, len(seqs) + 1))


def test_concurrent_followups_get_stable_contiguous_ordinals(db) -> None:
    """A02: parallel follow-ups produce unique ordinals and one contiguous journal."""
    principal = make_principal(db)
    app = sessions_app(db)
    sid = app.create(principal, principal.default_workspace_id, session_body())["session_id"]
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for i in range(6):
                app.send(principal, sid, {"content": f"m{n}-{i}"}, idempotency_key=f"k{n}-{i}")
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    seqs = _seqs(db, sid)
    assert seqs == list(range(1, len(seqs) + 1))
    ordinals = db.read(lambda u: sorted(t["ordinal"] for t in u.find("turns", {"session_id": sid})))
    assert ordinals == list(range(1, 49))
    # Only the head queued Turn is dispatched while others wait in order.
    jobs = db.read(lambda u: u.find("jobs", {"kind": "turn.dispatch"}))
    assert len(jobs) == 1


def test_journal_is_append_only(db) -> None:
    principal = make_principal(db)
    sid = sessions_app(db).create(principal, principal.default_workspace_id, session_body())[
        "session_id"
    ]
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction() as uow:
            uow.update_where("session_events", {"session_id": sid}, {"actor": "tamper"})
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction() as uow:
            uow.conn.execute("DELETE FROM session_events WHERE session_id = %s", (sid,))


def test_accepted_intent_replay_and_conflict(db) -> None:
    """A01: lost response -> same key returns same IDs; changed body conflicts."""
    principal = make_principal(db)
    app = sessions_app(db)
    wsp = principal.default_workspace_id
    body = session_body(message={"content": "do the thing"})
    first = app.create(principal, wsp, body, idempotency_key="create-1")
    again = app.create(principal, wsp, body, idempotency_key="create-1")
    assert again["session_id"] == first["session_id"] and again["turn_id"] == first["turn_id"]
    assert db.read(lambda u: u.count("sessions")) == 1
    assert db.read(lambda u: u.count("jobs", {"kind": "turn.dispatch"})) == 1
    with pytest.raises(DomainError) as err:
        app.create(
            principal, wsp, session_body(message={"content": "other"}), idempotency_key="create-1"
        )
    assert err.value.code == "idempotency_conflict"


def test_command_atomicity_all_or_nothing(db) -> None:
    """A03: a failure after projection/event/Job writes leaves nothing behind."""
    principal = make_principal(db)
    app = sessions_app(db)
    sid = app.create(principal, principal.default_workspace_id, session_body())["session_id"]
    before = db.read(
        lambda u: (u.count("messages"), u.count("turns"), u.count("jobs"), u.watermark(sid))
    )
    with pytest.raises(RuntimeError):
        with db.transaction() as uow:
            session = uow.get("sessions", sid, lock=True)
            app._accept(uow, principal, session, {"content": "x"}, actor=principal.user_id)
            raise RuntimeError("crash between projection and commit")
    after = db.read(
        lambda u: (u.count("messages"), u.count("turns"), u.count("jobs"), u.watermark(sid))
    )
    assert before == after


def test_db_constraints_enforce_one_active_turn_and_terminal_immutability(db) -> None:
    principal = make_principal(db)
    app = sessions_app(db)
    sid = app.create(principal, principal.default_workspace_id, session_body())["session_id"]
    t1 = app.send(principal, sid, {"content": "a"})["turn_id"]
    t2 = app.send(principal, sid, {"content": "b"})["turn_id"]
    db.run(lambda u: u.update("turns", t1, {"state": "preparing"}))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.run(lambda u: u.update("turns", t2, {"state": "preparing"}))
    db.run(lambda u: u.update("turns", t1, {"state": "failed"}))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.run(lambda u: u.update("turns", t1, {"state": "succeeded"}))


def test_reads_are_pure_and_watermark_consistent(db) -> None:
    """A10: every read runs in a READ ONLY snapshot; watermark matches returned state."""
    principal = make_principal(db)
    app = sessions_app(db)
    sid = app.create(
        principal, principal.default_workspace_id, session_body(message={"content": "go"})
    )["session_id"]
    q = Queries(db)

    def counts():
        return db.read(
            lambda u: tuple(
                u.count(t)
                for t in ("sessions", "turns", "jobs", "session_events", "executor_leases")
            )
        )

    before = counts()
    for _ in range(3):
        view = q.session(principal, sid)
        events = q.events(principal, sid)
        q.messages(principal, sid)
        q.turns(principal, sid)
        assert events["items"][-1]["seq"] == events["event_watermark"] == view["event_watermark"]
    assert counts() == before
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        db.read(lambda u: u.insert("login_attempts", {"email": "x", "succeeded": False}))


def test_filtered_events_report_scanned_watermark_and_cursor_errors(db) -> None:
    principal = make_principal(db)
    app = sessions_app(db)
    sid = app.create(
        principal, principal.default_workspace_id, session_body(message={"content": "go"})
    )["session_id"]
    q = Queries(db)
    page = q.events(principal, sid, types=["turn.queued"])
    assert [e["type"] for e in page["items"]] == ["turn.queued"]
    assert page["next_after"] == page["event_watermark"]
    with pytest.raises(DomainError) as err:
        q.events(principal, sid, after=page["event_watermark"] + 5)
    assert err.value.code == "invalid_cursor"


def test_cross_owner_access_is_not_found(db) -> None:
    alice, bob = make_principal(db), make_principal(db)
    app = sessions_app(db)
    sid = app.create(alice, alice.default_workspace_id, session_body())["session_id"]
    for call in (
        lambda: Queries(db).session(bob, sid),
        lambda: app.send(bob, sid, {"content": "hi"}),
        lambda: app.create(bob, alice.default_workspace_id, session_body()),
    ):
        with pytest.raises(DomainError) as err:
            call()
        assert err.value.code == "not_found"
