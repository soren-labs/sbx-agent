"""Control-plane unit tests for public session payload."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from control.backend import LocalProcessBackend, SandboxSpec
from control.service import ControlPlane, format_sse
from control.store import InMemoryStore


def test_public_cost_uses_sandbox_seconds() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    plane = ControlPlane(
        backend,
        store,
        [
            sys.executable,
            str(Path(__file__).resolve().parents[3] / "tests" / "fakes" / "stub_runner.py"),
        ],
        clock=lambda: datetime(2026, 9, 13, 12, 0, 30, tzinfo=UTC),
    )
    handle = backend.create(SandboxSpec(tags={"owner": "sbx"}))
    created = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    from control.store import SessionRecord, empty_usage

    rec = SessionRecord(
        id="s",
        title="t",
        status="idle",
        created_at=created,
        updated_at=created,
        model="gpt-5.6-luna",
        turns=0,
        usage=empty_usage(),
        messages=[],
        owner="sbx",
        sandbox_id=handle.id,
        sandbox_root=str(handle.root),
        last_activity_at=created,
    )
    public = plane.public(rec)
    assert public["sandbox_seconds"] == 30.0
    assert public["cost_estimate_usd"] > 0
    rec.status = "closed"
    rec.ended_at = created + timedelta(seconds=12)
    public = plane.public(rec)
    assert public["sandbox_seconds"] == 12.0
    backend.terminate(handle)


def test_public_terminal_without_ended_at_uses_updated_at() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    now = datetime(2026, 9, 13, 12, 0, 30, tzinfo=UTC)
    plane = ControlPlane(
        backend,
        store,
        [
            sys.executable,
            str(Path(__file__).resolve().parents[3] / "tests" / "fakes" / "stub_runner.py"),
        ],
        clock=lambda: now,
    )
    created = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    updated = datetime(2026, 9, 13, 12, 0, 12, tzinfo=UTC)
    from control.store import SessionRecord, empty_usage

    rec = SessionRecord(
        id="s",
        title="t",
        status="closed",
        created_at=created,
        updated_at=updated,
        model="gpt-5.6-luna",
        turns=0,
        usage=empty_usage(),
        messages=[],
        owner="sbx",
        ended_at=None,
        last_activity_at=updated,
    )
    for status in ("closed", "timed_out", "lost"):
        rec.status = status
        rec.ended_at = None
        public = plane.public(rec)
        assert public["sandbox_seconds"] == 12.0
        assert public["status"] == status
    rec.status = "closed"
    rec.ended_at = None
    rec.updated_at = created
    public = plane.public(rec)
    assert public["sandbox_seconds"] == 0.0


def test_format_sse_uses_line_number_and_type() -> None:
    frame = format_sse(3, {"type": "sbx.turn_started", "n": 1})
    assert frame.startswith("id: 3\n")
    assert "event: sbx.turn_started\n" in frame
    assert '"n": 1' in frame
    assert frame.endswith("\n\n")


# --------------------------------------------------------------- SOR-139


def _reconcile_plane() -> tuple[ControlPlane, LocalProcessBackend, InMemoryStore]:
    from control.run_store import InMemoryRunStore, RunLedger

    backend = LocalProcessBackend()
    store = InMemoryStore()
    plane = ControlPlane(
        backend,
        store,
        [
            sys.executable,
            str(Path(__file__).resolve().parents[3] / "tests" / "fakes" / "stub_runner.py"),
        ],
        run_ledger=RunLedger(InMemoryRunStore()),
    )
    return plane, backend, store


_SUCCESS_PAYLOAD = {
    "n": 1,
    "status": "success",
    "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 4},
    "message": "turn one done",
    "exit_code": 0,
}


def _stranded_session(
    backend: LocalProcessBackend,
    store: InMemoryStore,
    *,
    payload: dict | None,
    session_id: str = "s",
):
    """A ``running`` record bound to a live sandbox with no in-process watcher.

    That pair is exactly the post-cutover state SOR-139 describes: the
    provider finished (or not) while the watching container was drained.
    """
    from control.store import SessionRecord, empty_usage

    handle = backend.create(SandboxSpec(tags={"session_id": session_id, "owner": "sbx"}))
    if payload is not None:
        import json

        (handle.root / "turns").mkdir(parents=True, exist_ok=True)
        (handle.root / "turns" / "1.json").write_text(json.dumps(payload))
    now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
    store.put(
        SessionRecord(
            id=session_id,
            title="t",
            status="running",
            created_at=now,
            updated_at=now,
            model="gpt-5.6-luna",
            turns=0,
            usage=empty_usage(),
            messages=[],
            owner="sbx",
            sandbox_id=handle.id,
            sandbox_root=str(handle.root),
            sandbox_tags=dict(handle.tags),
            current_turn_id="turn-1",
            current_turn_n=1,
            last_activity_at=now,
        )
    )
    return handle


def test_reconcile_turn_finalizes_stranded_success() -> None:
    plane, backend, store = _reconcile_plane()
    handle = _stranded_session(backend, store, payload=_SUCCESS_PAYLOAD)
    assert plane.run_ledger is not None
    plane.run_ledger.begin(agent_id="s", n=1)

    assert plane.reconcile_turn("s") is True

    rec = store.get("s")
    assert rec is not None
    assert rec.status == "idle"
    assert rec.current_turn_id is None
    assert rec.current_turn_n is None
    assert rec.turns == 1
    assert rec.messages[-1] == {
        "role": "assistant",
        "text": "turn one done",
        "turn_id": "turn-1",
        "ts": rec.messages[-1]["ts"],
    }
    run = plane.run_ledger.get("s", 1)
    assert run is not None
    assert run.status == "FINISHED"
    assert run.result_text == "turn one done"
    assert run.terminal
    backend.terminate(handle)


def test_reconcile_turn_skips_watched_session() -> None:
    plane, backend, store = _reconcile_plane()
    handle = _stranded_session(backend, store, payload=_SUCCESS_PAYLOAD)
    plane._live["s"] = object()  # another turn's watcher owns this session

    assert plane.reconcile_turn("s") is False
    rec = store.get("s")
    assert rec is not None and rec.status == "running"
    backend.terminate(handle)


def test_reconcile_turn_waits_for_turn_evidence() -> None:
    # Live sandbox, no turns/1.json yet: the provider is still writing —
    # absent evidence must not finalize anything.
    plane, backend, store = _reconcile_plane()
    handle = _stranded_session(backend, store, payload=None)
    plane.run_ledger.begin(agent_id="s", n=1)

    assert plane.reconcile_turn("s") is False
    rec = store.get("s")
    assert rec is not None and rec.status == "running"
    run = plane.run_ledger.get("s", 1)
    assert run is not None and run.status == "RUNNING"
    backend.terminate(handle)


def test_reconcile_turn_dead_sandbox_waits_for_reaper() -> None:
    # Evidence exists but the sandbox is gone: the reaper's lost/timed_out
    # transition owns the session; reconciliation must not race it.
    plane, backend, store = _reconcile_plane()
    handle = _stranded_session(backend, store, payload=_SUCCESS_PAYLOAD)
    backend.terminate(handle)

    assert plane.reconcile_turn("s") is False
    rec = store.get("s")
    assert rec is not None and rec.status == "running"


def test_reconcile_turn_is_a_noop_for_idle_or_missing() -> None:
    plane, backend, store = _reconcile_plane()
    handle = _stranded_session(backend, store, payload=_SUCCESS_PAYLOAD)
    rec = store.get("s")
    assert rec is not None
    rec.status = "idle"
    rec.current_turn_id = None
    rec.current_turn_n = None
    store.put(rec)

    assert plane.reconcile_turn("s") is False
    assert plane.reconcile_turn("nobody") is False
    backend.terminate(handle)


def test_reconcile_turns_settles_only_stranded_sessions() -> None:
    plane, backend, store = _reconcile_plane()
    h1 = _stranded_session(backend, store, payload=_SUCCESS_PAYLOAD, session_id="a")
    h2 = _stranded_session(backend, store, payload=None, session_id="b")

    settled = plane.reconcile_turns()

    assert settled == ["a"]
    assert store.get("a").status == "idle"
    assert store.get("b").status == "running"
    backend.terminate(h1)
    backend.terminate(h2)


def test_stop_reconciles_before_cancelling() -> None:
    # A stop landing after the watcher died must not rewrite the provider's
    # success to CANCELLED.
    plane, backend, store = _reconcile_plane()
    handle = _stranded_session(backend, store, payload=_SUCCESS_PAYLOAD)
    plane.run_ledger.begin(agent_id="s", n=1)

    assert plane.stop("s") == "idle"

    run = plane.run_ledger.get("s", 1)
    assert run is not None and run.status == "FINISHED"
    backend.terminate(handle)


def test_post_message_reconciles_then_dispatches_follow_up() -> None:
    # The stranded record must not 409: the freed agent takes a new turn.
    plane, backend, store = _reconcile_plane()
    handle = _stranded_session(backend, store, payload=_SUCCESS_PAYLOAD)

    turn_id = plane.post_message("s", "next")

    assert turn_id == "turn-2"
    import time

    deadline = datetime.now(tz=UTC) + timedelta(seconds=15)
    while datetime.now(tz=UTC) < deadline:
        rec = store.get("s")
        if rec is not None and rec.status == "idle":
            break
        time.sleep(0.05)
    rec = store.get("s")
    assert rec is not None and rec.status == "idle"
    backend.terminate(handle)


def test_settle_orphaned_runs_persists_terminal_for_open_runs() -> None:
    plane, backend, store = _reconcile_plane()
    handle = _stranded_session(backend, store, payload=None)
    plane.run_ledger.begin(agent_id="s", n=1)
    plane.run_ledger.begin(agent_id="s", n=2)
    plane.run_ledger.finish("s", 2, status="FINISHED")

    settled = plane.settle_orphaned_runs("s", session_status="timed_out")

    assert settled == [1]
    run = plane.run_ledger.get("s", 1)
    assert run is not None and run.status == "EXPIRED"
    assert run.error is not None and run.error["code"] == "timeout"
    untouched = plane.run_ledger.get("s", 2)
    assert untouched is not None and untouched.status == "FINISHED"
    backend.terminate(handle)
