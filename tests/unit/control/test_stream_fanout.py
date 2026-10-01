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
