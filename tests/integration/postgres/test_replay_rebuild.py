"""A03: rebuilt state from the committed journal matches typed projections."""

from __future__ import annotations

from control.application.replay import replay_session
from tests.support.factories import make_principal, session_body, sessions_app


def test_journal_replay_matches_projections(db) -> None:
    principal = make_principal(db)
    app = sessions_app(db)
    sid = app.create(
        principal, principal.default_workspace_id, session_body(message={"content": "a"})
    )["session_id"]
    t2 = app.send(principal, sid, {"content": "b"})["turn_id"]
    app.send(principal, sid, {"content": "note", "routing": "note"})
    app.cancel_turn(principal, t2)
    app.set_lifecycle(principal, sid, "archived")

    def check(uow):
        events = uow.find("session_events", {"session_id": sid}, order="seq")
        rebuilt = replay_session(events)
        session = uow.get("sessions", sid)
        turns = {t["id"]: t["state"] for t in uow.find("turns", {"session_id": sid})}
        assert rebuilt["lifecycle"] == session["lifecycle"] == "archived"
        assert rebuilt["turns"] == turns
        assert rebuilt["messages"] == uow.count("messages", {"session_id": sid})
        assert rebuilt["last_seq"] == uow.watermark(sid)

    db.read(check)
