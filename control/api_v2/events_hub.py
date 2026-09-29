"""Shared per-session event fanout for the V2 SSE route (SOR-268).

Before this module each ``GET /v2/sessions/{id}/events`` client owned a
private ``tail -F`` exec, a private status poll (task_store.get +
workspace fetch + aggregate status + reconcile probe every 0.5s), and a
private sandbox ``poll`` at 20 Hz — all on the ASGI event loop. Eight
clients multiplied every one of those remote calls by eight; twenty by
twenty — the SOR-260 B3 starvation finding.

A ``SessionEventsHub`` owns ONE background thread per session. It holds
the tail exec, the status settle, the provisioning wait, and the replay
resolution; each subscriber is just a ``queue.Queue`` the hub fans parsed
frames into. The wire contract is unchanged: ``id:`` is the events.jsonl
line number, a fresh ``session.status`` frame (no id) opens every
connection, ``Last-Event-ID`` resumes at ``start_line``, keepalives are
emitted by the per-client generator when its queue is idle, a tail EOF
closes the stream after a final status frame, and an unreachable/terminal
sandbox falls back to durable replay + indefinite status ticks.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from control.api_v1.routes import _parse_event_line
from control.api_v2 import events as _events
from control.service import format_sse

_STATUS_POLL_S = 0.5
_HANDLE_POLL_S = 1.0
_LOOP_IDLE_S = 0.25
# Bound on retained parsed frames per session; a resume older than the
# oldest retained line just starts at the floor (same as the events.jsonl
# itself being truncated).
_MAX_FRAMES = 50_000
# A client-less hub is torn down after this idle window — frees the tail
# exec, reader thread, and the frame ring.
_IDLE_TTL_S = 30.0

_CLOSE = object()  # queue sentinel: end-of-stream for current subscribers


class _Subscriber:
    __slots__ = ("q", "start_line")

    def __init__(self, start_line: int) -> None:
        self.q: queue.Queue[Any] = queue.Queue()
        self.start_line = start_line


class SessionEventsHub:
    """One tail + one poller per session; subscribers get filtered queues.

    The injected callables carry the route's dependencies so the hub stays
    free of api_v1/task-layer imports:

    - ``record_probe()`` -> durable TaskRecord (or None when gone)
    - ``is_terminal_record(record)`` -> bool (task-level terminal set)
    - ``status_bits()`` -> ``(status, phase, sse_frame_text)``
    - ``live_handle()`` -> ``(handle, poll) | (None, None)`` — sandbox +
      its liveness probe, resolved from the bound agent
    - ``agent_terminal()`` -> bool — bound agent record is gone/terminal
      or every known run is terminal (sandbox will never produce again)
    - ``start_tail(handle)`` -> ``Process`` — the ``tail -F`` exec
    - ``replay_lines()`` -> ``list[str]`` — events.jsonl contents or the
      stitched durable run-activity frames (already normalized-entry
      dicts are NOT passed; the hub parses raw lines itself)
    - ``replay_entries()`` -> ``list[dict]`` — ``{"id": int, "event": dict}``
      transcript entries used when there is no events.jsonl at all
    """

    def __init__(
        self,
        session_id: str,
        *,
        record_probe: Callable[[], Any],
        is_terminal_record: Callable[[Any], bool],
        status_bits: Callable[[], tuple[str, str, str]],
        live_handle: Callable[[], tuple[Any, Any]],
        agent_terminal: Callable[[], bool],
        start_tail: Callable[[Any], Any],
        replay_lines: Callable[[], list[str]],
        replay_entries: Callable[[], list[dict[str, Any]]],
    ) -> None:
        self.session_id = session_id
        self._record_probe = record_probe
        self._is_terminal_record = is_terminal_record
        self._status_bits = status_bits
        self._live_handle = live_handle
        self._agent_terminal = agent_terminal
        self._start_tail = start_tail
        self._replay_lines = replay_lines
        self._replay_entries = replay_entries

        self._lock = threading.Lock()
        self._subs: list[_Subscriber] = []
        self._frames: list[tuple[int, str]] = []  # (lineno, formatted frame)
        self._lineno = 0
        self._current_turn = 0
        self._status: tuple[str, str, str] | None = None  # last status tick
        # Serializes cold ``opening_status`` computes: a reconnect herd
        # (tail EOF → N clients attach at once) pays ONE ``status_bits``
        # remote fan-in, not N — SOR-271 round 2.
        self._status_compute_lock = threading.Lock()
        self._state = "waiting"  # waiting | live | replay
        self._inbox: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self._proc: Any = None
        self._dead = threading.Event()
        self._last_client_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"sbx-v2-hub-{session_id[-8:]}"
        )
        self._thread.start()

    # ------------------------------------------------------------- client API

    def subscribe(self, start_line: int) -> tuple[_Subscriber, str | None, list[str]]:
        """Register a subscriber; returns (sub, opening_status, backlog)."""
        sub = _Subscriber(start_line)
        with self._lock:
            self._last_client_at = time.monotonic()
            backlog = [frame for lineno, frame in self._frames if lineno >= start_line]
            opening = self._status[2] if self._status is not None else None
            self._subs.append(sub)
        return sub, opening, backlog

    def unsubscribe(self, sub: _Subscriber) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
            self._last_client_at = time.monotonic()

    # --------------------------------------------------------------- fanout

    def _broadcast(self, item: Any) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            sub.q.put(item)

    def _append_frame(self, lineno: int, norm: dict[str, Any]) -> None:
        frame = format_sse(lineno, norm)
        with self._lock:
            self._frames.append((lineno, frame))
            if len(self._frames) > _MAX_FRAMES:
                del self._frames[: len(self._frames) - _MAX_FRAMES]
            subs = [sub for sub in self._subs if lineno >= sub.start_line]
        for sub in subs:
            sub.q.put(frame)

    def _status_tick(self) -> None:
        try:
            st, ph, frame = self._status_bits()
        except Exception:
            return
        if self._status is not None and self._status[:2] == (st, ph):
            return
        self._status = (st, ph, frame)
        self._broadcast(frame)

    def opening_status(self) -> str | None:
        """Current status frame — computed synchronously if never polled.

        The compute is deduped under ``_status_compute_lock``: concurrent
        first-connect callers wait for the one in-flight ``status_bits``
        and then read the populated ``_status`` instead of each paying
        the remote fan-in."""
        if self._status is not None:
            return self._status[2]
        with self._status_compute_lock:
            if self._status is None:
                try:
                    st, ph, frame = self._status_bits()
                    self._status = (st, ph, frame)
                except Exception:
                    return None
            return self._status[2]

    # ------------------------------------------------------------ internals

    def _reset_ingest(self) -> None:
        """Start a fresh ingest pass over events.jsonl.

        Every tail re-spawn or replay resolution re-reads the file from
        line 1, so numbering restarts — ``id`` stays the events.jsonl
        line number and a reconnecting ``Last-Event-ID`` resume dedups
        correctly. A continuing counter re-emits the whole transcript
        under shifted ids instead (SOR-268 review finding)."""
        self._lineno = 0
        self._current_turn = 0
        with self._lock:
            self._frames.clear()

    def _ingest_raw(self, raw: str) -> None:
        """Parse one raw events.jsonl line; fanout the normalized frame."""
        self._lineno += 1
        if not raw.strip():
            return
        obj = _parse_event_line(raw)
        self._current_turn = _events.track_turn(obj, self._current_turn)
        norm = _events.normalize(obj, self._current_turn)
        if norm is not None:
            self._append_frame(self._lineno, norm)

    def _resolve_replay(self) -> None:
        """Terminal / sandbox-unreachable replay: events.jsonl if readable,
        else the durable per-run transcripts — resolved ONCE per hub."""
        self._reset_ingest()
        lines: list[str] = []
        try:
            lines = self._replay_lines()
        except Exception:
            lines = []
        if lines:
            for raw in lines:
                self._ingest_raw(raw)
            return
        entries: list[dict[str, Any]] = []
        try:
            entries = self._replay_entries()
        except Exception:
            entries = []
        entries.sort(key=lambda e: e["id"])
        for entry in entries:
            obj = entry["event"]
            self._current_turn = _events.track_turn(obj, self._current_turn)
            norm = _events.normalize(obj, self._current_turn)
            if norm is not None:
                self._append_frame(int(entry["id"]), norm)

    def _spawn_tail(self, handle: Any) -> None:
        proc = self._start_tail(handle)
        self._proc = proc

        def _reader() -> None:
            try:
                for line in proc.stdout:
                    self._inbox.put(("line", line))
            except Exception:
                pass
            finally:
                self._inbox.put(("eof", None))

        threading.Thread(
            target=_reader, daemon=True, name=f"sbx-v2-hub-tail-{self.session_id[-8:]}"
        ).start()

    def _run(self) -> None:
        next_status = 0.0
        next_handle = 0.0
        try:
            while not self._dead.is_set():
                # Exit once every client has been gone for the idle TTL.
                with self._lock:
                    idle = not self._subs
                if idle and time.monotonic() - self._last_client_at > _IDLE_TTL_S:
                    return

                try:
                    kind, payload = self._inbox.get(timeout=_LOOP_IDLE_S)
                    if kind == "eof":
                        # Contract: one final status frame, then the stream
                        # closes (the client reconnects into replay).
                        try:
                            st, ph, frame = self._status_bits()
                            self._status = (st, ph, frame)
                            self._broadcast(frame)
                        except Exception:
                            pass
                        self._broadcast(_CLOSE)
                        self._proc = None
                        self._state = "waiting"
                        self._reset_ingest()
                        continue
                    self._ingest_raw(payload or "")
                except queue.Empty:
                    pass

                now = time.monotonic()
                if now >= next_status:
                    next_status = now + _STATUS_POLL_S
                    self._status_tick()

                if self._state == "waiting" and now >= next_handle:
                    next_handle = now + _HANDLE_POLL_S
                    record = None
                    try:
                        record = self._record_probe()
                    except Exception:
                        record = None
                    if record is not None and self._is_terminal_record(record):
                        self._state = "replay"
                        self._resolve_replay()
                        continue
                    if self._agent_terminal():
                        self._state = "replay"
                        self._resolve_replay()
                        continue
                    try:
                        handle, poll = self._live_handle()
                    except Exception:
                        handle, poll = None, None
                    if handle is not None and poll is not None and poll.alive:
                        try:
                            self._spawn_tail(handle)
                            self._state = "live"
                        except Exception:
                            pass
        finally:
            proc, self._proc = self._proc, None
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._broadcast(_CLOSE)


class SessionEventsHubRegistry:
    """``app.state``-resident hub table keyed by session id.

    ``acquire`` returns a live hub (creating or reviving as needed); hubs
    whose client count has been zero for ``_IDLE_TTL_S`` are reaped by
    their own loop and dropped here lazily.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hubs: dict[str, SessionEventsHub] = {}

    def acquire(self, session_id: str, factory: Callable[[], SessionEventsHub]) -> SessionEventsHub:
        with self._lock:
            hub = self._hubs.get(session_id)
            if hub is None or not hub._thread.is_alive():
                hub = self._hubs.pop(session_id, None)
                hub = factory()
                self._hubs[session_id] = hub
            return hub

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._hubs.pop(session_id, None)
