"""Live text delivery must not wait for a remote status probe."""

import queue
import threading
from types import SimpleNamespace

from control.api_v2.events_hub import SessionEventsHub


def test_tail_delivers_while_status_probe_is_blocked(monkeypatch):
    monkeypatch.setattr("control.api_v2.events_hub._STATUS_POLL_S", 0.01)
    feed = queue.Queue()
    started = threading.Event()
    blocked = threading.Event()
    release = threading.Event()
    calls = 0

    def status():
        nonlocal calls
        calls += 1
        if calls > 1:
            blocked.set()
            assert release.wait(5)
        return "running", "run", "data: {}\n\n"

    def lines():
        started.set()
        while (line := feed.get()) is not None:
            yield line

    hub = SessionEventsHub(
        "stream-test",
        record_probe=lambda: None,
        is_terminal_record=lambda _: False,
        status_bits=status,
        live_handle=lambda: (object(), SimpleNamespace(alive=True)),
        agent_terminal=lambda: False,
        start_tail=lambda _: SimpleNamespace(stdout=lines(), kill=lambda: feed.put(None)),
        replay_lines=lambda: [],
        replay_entries=lambda: [],
    )
    sub, _, _ = hub.subscribe(1)
    try:
        assert started.wait(3)
        assert blocked.wait(3)
        feed.put('{"type":"sbx.turn_started","n":1}')
        feed.put(
            '{"type":"item.updated","item":{"id":"m","type":"agent_message",'
            '"text":"你好","status":"in_progress"}}'
        )
        while True:
            frame = sub.q.get(timeout=1)
            if "你好" in frame:
                assert '"n":1' in frame.replace(" ", "")
                break
        assert not release.is_set()  # status is still blocked when text arrives
    finally:
        release.set()
        hub._dead.set()
        feed.put(None)
        hub._thread.join(timeout=3)
        hub.unsubscribe(sub)


def test_subscriber_notification_follows_queue_visibility():
    from control.api_v2.events_hub import _Subscriber

    sub = _Subscriber(1)
    seen = []
    sub.wake = lambda: seen.append(sub.q.get_nowait())
    sub.put("first")
    sub.put("second")
    assert seen == ["first", "second"]


def test_finished_turn_keeps_live_tail_for_followup(monkeypatch):
    monkeypatch.setattr("control.api_v2.events_hub._STATUS_POLL_S", 0.01)
    feed = queue.Queue()
    started = threading.Event()

    def lines():
        started.set()
        while (line := feed.get()) is not None:
            yield line

    hub = SessionEventsHub(
        "followup",
        record_probe=lambda: SimpleNamespace(status="finished"),
        is_terminal_record=lambda _: True,
        status_bits=lambda: ("finished", "finished", "data: {}\n\n"),
        live_handle=lambda: (object(), SimpleNamespace(alive=True)),
        agent_terminal=lambda: True,
        start_tail=lambda _: SimpleNamespace(stdout=lines(), kill=lambda: feed.put(None)),
        replay_lines=lambda: [],
        replay_entries=lambda: [],
    )
    sub, _, _ = hub.subscribe(1)
    try:
        assert started.wait(3)
        feed.put('{"type":"sbx.turn_started","n":2}')
        feed.put(
            '{"type":"item.updated","item":{"id":"followup",'
            '"type":"agent_message","text":"partial"}}'
        )
        while True:
            frame = sub.q.get(timeout=1)
            if "partial" in frame:
                assert '"n":2' in frame.replace(" ", "")
                break
    finally:
        hub._dead.set()
        feed.put(None)
        hub._thread.join(timeout=3)
        hub.unsubscribe(sub)


def test_replay_can_resume_live_on_recovered_followup(monkeypatch):
    monkeypatch.setattr("control.api_v2.events_hub._STATUS_POLL_S", 0.01)
    state = {"running": False}
    replayed = threading.Event()
    started = threading.Event()
    feed = queue.Queue()

    old_lines = [
        '{"type":"sbx.turn_started","n":1}',
        '{"type":"item.completed","item":{"id":"old","type":"agent_message","text":"old"}}',
    ]

    def replay():
        replayed.set()
        return old_lines

    def lines():
        started.set()
        while (line := feed.get()) is not None:
            yield line

    hub = SessionEventsHub(
        "recovered-followup",
        record_probe=lambda: SimpleNamespace(status="finished"),
        is_terminal_record=lambda _: True,
        status_bits=lambda: ("running" if state["running"] else "finished", "run", "data: {}\n\n"),
        live_handle=lambda: (
            (object(), SimpleNamespace(alive=True)) if state["running"] else (None, None)
        ),
        agent_terminal=lambda: True,
        start_tail=lambda _: SimpleNamespace(stdout=lines(), kill=lambda: feed.put(None)),
        replay_lines=replay,
        replay_entries=lambda: [],
    )
    sub, _, _ = hub.subscribe(1)
    try:
        assert replayed.wait(3)
        old_ids = []
        while len(old_ids) < 2:
            frame = sub.q.get(timeout=1)
            if frame.startswith("id:"):
                old_ids.append(int(frame.split("\n", 1)[0][3:]))
        assert old_ids == [1, 2]
        state["running"] = True
        assert started.wait(3)
        for line in old_lines:
            feed.put(line)
        feed.put('{"type":"sbx.turn_started","n":2}')
        feed.put(
            '{"type":"item.updated","item":{"id":"recover",'
            '"type":"agent_message","text":"live again"}}'
        )
        new_ids = []
        while True:
            frame = sub.q.get(timeout=1)
            if frame.startswith("id:"):
                new_ids.append(int(frame.split("\n", 1)[0][3:]))
            if "live again" in frame:
                assert '"n":2' in frame.replace(" ", "")
                break
        assert new_ids == [3, 4]
    finally:
        hub._dead.set()
        feed.put(None)
        hub._thread.join(timeout=3)
        hub.unsubscribe(sub)
