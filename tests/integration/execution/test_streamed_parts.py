"""Streamed reply parts through ingest, and evidence acknowledgement while they flow."""

from __future__ import annotations

import pytest
from control.application.ingest import ingest
from control.runtime_client.client import RuntimeClient
from tests.support.factories import make_principal, session_body
from tests.support.stack import Stack


@pytest.fixture
def stack(db, tmp_path):
    s = Stack(db, tmp_path)
    yield s
    s.shutdown()


def _start(stack, text):
    principal = make_principal(stack.db)
    created = stack.sessions.create(
        principal, principal.default_workspace_id, session_body(message={"content": text})
    )
    return principal, created["session_id"], created["turn_id"]


def test_append_deltas_grow_one_part_and_a_replayed_revision_never_duplicates(stack) -> None:
    principal, sid, turn_id = _start(stack, "x [hang]")
    stack.drive(lambda: stack.turn(turn_id)["state"] == "running")
    lease = stack.db.read(lambda u: u.find_one("executor_leases", {"session_id": sid}))
    execution = stack.db.read(lambda u: u.find_one("executions", {"turn_id": turn_id}))
    batch = stack.connector.channel(lease).events(0)
    stack.db.run(
        lambda u: ingest(u, lease, batch["runtime_epoch"], batch["items"], stack.execution.hooks)
    )
    seq = batch["last_local_seq"]

    def part(n: int, kind: str, revision: int, mode: str, content: str) -> dict:
        payload = {
            "part_key": "stream-1",
            "kind": "text",
            "mode": mode,
            "revision": revision,
            "content": content,
        }
        return {
            "local_seq": seq + n,
            "execution_id": execution["id"],
            "type": kind,
            "payload": payload,
        }

    deltas = [
        part(1, "message.part_added", 1, "append", "Hel"),
        part(2, "message.part_updated", 2, "append", "lo"),
        # The same revision observed again (a runtime retry) must not append twice.
        part(3, "message.part_updated", 2, "append", "lo"),
        part(4, "message.part_updated", 3, "append", ", world"),
    ]
    stack.db.run(lambda u: ingest(u, lease, batch["runtime_epoch"], deltas, stack.execution.hooks))
    # The whole batch delivered again changes nothing.
    again = stack.db.run(
        lambda u: ingest(u, lease, batch["runtime_epoch"], deltas, stack.execution.hooks)
    )
    assert again.applied == 0

    def streamed() -> dict:
        items = stack.queries.messages(principal, sid)["items"]
        return next(p for m in items for p in m["parts"] if p["key"] == "stream-1")

    growing = streamed()
    assert (growing["content"], growing["revision"], growing["sealed"]) == (
        "Hello, world",
        3,
        False,
    )
    # The completed block is authoritative and replaces what was streamed.
    final = [part(5, "message.part_updated", 4, "replace", "Hello, world!")]
    stack.db.run(lambda u: ingest(u, lease, batch["runtime_epoch"], final, stack.execution.hooks))
    done = streamed()
    assert (done["content"], done["revision"]) == ("Hello, world!", 4)
    # Real times bound how long the part took; the start never moves.
    assert done["created_at"] == growing["created_at"]
    assert done["created_at"] <= growing["updated_at"] <= done["updated_at"]
    journal = [e for e in stack.events(sid) if e["payload"].get("part_key") == "stream-1"]
    assert [e["payload"]["revision"] for e in journal] == [1, 2, 2, 3, 4]


def test_acks_wait_for_a_pause_and_everything_is_acknowledged_by_the_end(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    acks: list[int] = []
    real_ack = RuntimeClient.ack

    def counting_ack(self: RuntimeClient, runtime_epoch: str, through: int) -> dict:
        acks.append(through)
        return real_ack(self, runtime_epoch, through)

    monkeypatch.setattr(RuntimeClient, "ack", counting_ack)
    _, sid, turn_id = _start(stack, "hello [slow:1] [write:a.txt=one]")
    stack.drive(stack.turn_done(turn_id))
    assert stack.turn(turn_id)["state"] == "succeeded"
    lease = stack.db.read(lambda u: u.find_one("executor_leases", {"session_id": sid}))
    spool = stack.connector.channel(lease).events(0)
    runtime_events = [e for e in stack.events(sid) if e["source"] == "runtime"]
    # Nothing is left unacknowledged, so a checkpoint is never refused for it.
    assert spool["acked"] == spool["last_local_seq"] == len(runtime_events)
    # Acks are monotonic and far fewer than one per ingested observation.
    assert acks == sorted(acks) and acks[-1] == spool["last_local_seq"]
    assert len(acks) < len(runtime_events)
