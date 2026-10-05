"""Session/Message/Turn commands: routing, cancellation, retry, lifecycle."""

from __future__ import annotations

import pytest
from control.application.sessions import finish_turn
from control.domain.errors import DomainError
from tests.support.factories import make_principal, session_body, sessions_app


@pytest.fixture
def ctx(db):
    principal = make_principal(db)
    app = sessions_app(db)
    sid = app.create(principal, principal.default_workspace_id, session_body())["session_id"]
    return db, principal, app, sid


def _turn(db, turn_id):
    return db.read(lambda u: u.get("turns", turn_id))


def test_disabled_harness_is_rejected(db) -> None:
    principal = make_principal(db)
    with pytest.raises(DomainError) as err:
        sessions_app(db).create(
            principal,
            principal.default_workspace_id,
            session_body(harness={"provider_id": "claude"}),
        )
    assert err.value.code == "unsupported_capability"


def test_note_creates_no_turn_and_steer_requires_capability(ctx) -> None:
    db, principal, app, sid = ctx
    note = app.send(principal, sid, {"content": "fyi", "routing": "note"})
    assert "turn_id" not in note
    with pytest.raises(DomainError) as err:
        app.send(principal, sid, {"content": "steer", "routing": "steer"})
    assert err.value.code == "unsupported_capability"
    queued = app.send(principal, sid, {"content": "steer", "routing": "steer", "fallback": "queue"})
    assert queued["routing"] == "queue" and queued["turn_id"]
    with pytest.raises(DomainError):
        app.send(principal, sid, {"content": "x", "settings": {"effort": "high"}})


def test_cancel_queued_is_terminal_and_idempotent(ctx) -> None:
    db, principal, app, sid = ctx
    turn_id = app.send(principal, sid, {"content": "work"})["turn_id"]
    first = app.cancel_turn(principal, turn_id)
    assert first["turn"]["state"] == "cancelled"
    assert app.cancel_turn(principal, turn_id)["turn"]["state"] == "cancelled"
    job = db.read(lambda u: u.find_one("jobs", {"kind": "turn.dispatch", "turn_id": turn_id}))
    assert job["state"] == "cancelled"


def test_committed_success_wins_over_later_cancel(ctx) -> None:
    """A07 order 1: success committed first -> cancel returns that verdict."""
    db, principal, app, sid = ctx
    turn_id = app.send(principal, sid, {"content": "work"})["turn_id"]

    def succeed(uow):
        session = uow.get("sessions", sid, lock=True)
        uow.update("turns", turn_id, {"state": "preparing"})
        uow.update("turns", turn_id, {"state": "running"})
        finish_turn(
            uow,
            session,
            uow.get("turns", turn_id),
            "succeeded",
            actor="worker",
            evidence_complete=True,
        )

    db.run(succeed)
    assert app.cancel_turn(principal, turn_id)["turn"]["state"] == "succeeded"


def test_cancel_first_prevents_business_success(ctx) -> None:
    """A07 order 2: cancellation committed first -> late success cannot win."""
    db, principal, app, sid = ctx
    turn_id = app.send(principal, sid, {"content": "work"})["turn_id"]
    db.run(
        lambda u: (
            u.update("turns", turn_id, {"state": "preparing"}),
            u.update("turns", turn_id, {"state": "running"}),
        )
    )
    assert app.cancel_turn(principal, turn_id)["turn"]["state"] == "cancelling"

    def late_success(uow):
        session = uow.get("sessions", sid, lock=True)
        finish_turn(uow, session, uow.get("turns", turn_id), "succeeded", actor="worker")

    with pytest.raises(DomainError) as err:
        db.run(late_success)
    assert err.value.code == "invalid_transition"


def test_retry_links_new_turn_and_unknown_needs_ack(ctx) -> None:
    db, principal, app, sid = ctx
    turn_id = app.send(principal, sid, {"content": "work"})["turn_id"]

    def interrupt(uow):
        session = uow.get("sessions", sid, lock=True)
        uow.update("turns", turn_id, {"state": "preparing"})
        uow.update("turns", turn_id, {"state": "running"})
        finish_turn(
            uow,
            session,
            uow.get("turns", turn_id),
            "interrupted",
            actor="worker",
            reason="outcome_unknown",
            evidence_complete=False,
        )

    db.run(interrupt)
    with pytest.raises(DomainError) as err:
        app.retry_turn(principal, turn_id)
    assert err.value.code == "outcome_unknown"
    # Unknown blocks new dispatch until acknowledged.
    blocked = app.send(principal, sid, {"content": "next"})["turn_id"]
    assert db.read(lambda u: u.count("jobs", {"kind": "turn.dispatch", "turn_id": blocked})) == 0
    app.acknowledge_unknown(principal, turn_id)
    assert db.read(lambda u: u.count("jobs", {"kind": "turn.dispatch", "turn_id": blocked})) == 1
    retried = app.retry_turn(principal, turn_id, idempotency_key="r1")
    assert _turn(db, retried["turn_id"])["retry_of_turn_id"] == turn_id
    assert app.retry_turn(principal, turn_id, idempotency_key="r1")["turn_id"] == retried["turn_id"]


def test_archive_unarchive_close(ctx) -> None:
    db, principal, app, sid = ctx
    app.set_lifecycle(principal, sid, "archived")
    with pytest.raises(DomainError):
        app.send(principal, sid, {"content": "work"})
    assert "turn_id" not in app.send(principal, sid, {"content": "note", "routing": "note"})
    app.set_lifecycle(principal, sid, "open")
    t = app.send(principal, sid, {"content": "work"})["turn_id"]
    t2 = app.send(principal, sid, {"content": "more"})["turn_id"]
    closed = app.set_lifecycle(principal, sid, "closed")
    assert closed["session"]["lifecycle"] == "closed"
    assert _turn(db, t)["state"] == "cancelled" and _turn(db, t2)["reason"] == "session_closed"
    with pytest.raises(DomainError):
        app.set_lifecycle(principal, sid, "open")
    types = db.read(
        lambda u: [e["type"] for e in u.find("session_events", {"session_id": sid}, order="seq")]
    )
    assert types.count("session.closed") == 1 and "session.archived" in types


def test_update_requires_expected_version(ctx) -> None:
    db, principal, app, sid = ctx
    with pytest.raises(DomainError) as err:
        app.update(principal, sid, {"title": "x", "expected_version": 99})
    assert err.value.code == "version_conflict"
    out = app.update(principal, sid, {"title": "renamed", "expected_version": 1})
    assert out["session"]["title"] == "renamed" and out["session"]["version"] == 2
