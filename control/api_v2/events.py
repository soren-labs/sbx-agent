"""SOR-256: normalized session event stream.

``GET /v2/sessions/{id}/events`` serves a normalized event vocabulary —
``session.status``, ``message.created``, ``activity.*``, ``usage.updated``,
``changes.updated``, ``delivery.updated``, ``session.completed``,
``session.failed`` — regardless of the provider's own event shape.

Per session, exactly one *feed* exists on this hub: one sandbox tail
(``tail -F events.jsonl``) plus one state poller (session/run/revision/
delivery stores), fan out to every subscriber — concurrent SSE clients never
multiply the remote polling. Each emitted frame carries a session-scoped
monotonic ``id`` (the feed sequence); ``Last-Event-ID`` replays from the
feed's bounded replay buffer, so reconnects are practical.
"""

from __future__ import annotations

import json
import queue as queue_mod
import threading
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from control.api_v1 import tasks as _tasks
from control.api_v2 import projection as proj
from control.sandbox_io import sandbox_env

# Bounded replay: the feed keeps the last N normalized events per session.
REPLAY_BUFFER = 1000
# A feed with no subscribers tears down after this grace window so a
# reconnect inside it still replays.
ORPHAN_TTL_S = 60.0


@dataclass
class SessionEvent:
    seq: int
    type: str
    data: dict[str, Any]

    def sse(self) -> str:
        return (
            f"id: {self.seq}\n"
            f"event: {self.type}\n"
            f"data: {json.dumps(self.data, ensure_ascii=False)}\n\n"
        )


def _parse_raw(raw: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def normalize_raw(obj: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Map one canonical events.jsonl row to 0..n normalized events.

    Sandboxed-transcript noise (thread/turn markers, runner bookkeeping)
    folds into the state-driven events the poller synthesizes instead.
    """
    t = obj.get("type")
    item = obj.get("item") if isinstance(obj.get("item"), dict) else None
    if t == "item.started" and item is not None:
        return [("activity.started", {"activity": proj._activity_view(0, obj) or {}})]
    if t == "item.updated" and item is not None:
        return [("activity.updated", {"activity": proj._activity_view(0, obj) or {}})]
    if t == "item.completed" and item is not None:
        return [("activity.completed", {"activity": proj._activity_view(0, obj) or {}})]
    if t == "turn.completed" and isinstance(obj.get("usage"), dict):
        return [("usage.updated", {"usage": obj["usage"], "run": obj.get("n")})]
    if t == "error":
        return [
            (
                "activity.completed",
                {
                    "activity": {
                        "id": "error",
                        "kind": "error",
                        "status": "failed",
                        "message": str(obj.get("message") or ""),
                    }
                },
            )
        ]
    return []


@dataclass
class _Snap:
    """One poll of the session's durable state — the diff input."""

    status: str = "queued"
    phase: str = "resolving"
    msg_count: int = 0
    msg_tail: list[dict[str, Any]] | None = None
    usage_key: tuple | None = None
    usage: dict[str, Any] | None = None
    changes_key: tuple | None = None
    changes: dict[str, Any] | None = None
    delivery_key: tuple | None = None
    delivery: dict[str, Any] | None = None
    terminal: str | None = None  # "finished" | "failed" | "cancelled"
    error: dict[str, Any] | None = None


class _Feed:
    """One session's tail+poller fan-out. Lives while subscribers or TTL."""

    def __init__(self, hub: SessionEventsHub, session_id: str) -> None:
        self.hub = hub
        self.session_id = session_id
        self.buffer: deque[SessionEvent] = deque(maxlen=REPLAY_BUFFER)
        self.subscribers: set[queue_mod.Queue] = set()
        self.seq = 0
        self.live = True
        self.orphaned_at: float | None = None
        self.proc: Any = None
        self.last_line = 0
        self.last_snap = _Snap()
        self.terminal_sent = False
        self.lock = threading.Lock()
        self._tail_thread = threading.Thread(
            target=self._tail_loop, daemon=True, name=f"sbx-v2-tail-{session_id}"
        )
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name=f"sbx-v2-poll-{session_id}"
        )

    def start(self) -> None:
        self._tail_thread.start()
        self._poll_thread.start()

    # -- emit / subscribe -------------------------------------------------

    def emit(self, etype: str, data: dict[str, Any]) -> SessionEvent | None:
        with self.lock:
            if not self.live:
                return None
            self.seq += 1
            event = SessionEvent(seq=self.seq, type=etype, data=data)
            self.buffer.append(event)
            subs = list(self.subscribers)
        for sub in subs:
            try:
                sub.put_nowait(event)
            except Exception:
                pass
        return event

    def subscribe(self, last_seq: int | None) -> queue_mod.Queue:
        """New subscriber queue; caller replays ``replay()`` then drains."""
        q: queue_mod.Queue = queue_mod.Queue()
        with self.lock:
            self.subscribers.add(q)
            self.orphaned_at = None
        return q

    def unsubscribe(self, q: queue_mod.Queue) -> None:
        with self.lock:
            self.subscribers.discard(q)
            if not self.subscribers and self.orphaned_at is None:
                self.orphaned_at = time.monotonic()

    def replay(self, last_seq: int | None) -> list[SessionEvent]:
        with self.lock:
            rows = list(self.buffer)
        if last_seq is None:
            return rows
        if rows and last_seq >= self.seq:
            return []
        out = [e for e in rows if e.seq > last_seq]
        # A last_id older than the buffer floor gets the retained window —
        # practical replay, never a silent gap.
        return out if out else rows

    def _orphan_expired(self) -> bool:
        with self.lock:
            at = self.orphaned_at
            empty = not self.subscribers
        return empty and at is not None and (time.monotonic() - at) > ORPHAN_TTL_S

    # -- state poller -------------------------------------------------------

    def _snapshot(self) -> _Snap | None:
        hub = self.hub
        record = hub.task_store.get(self.session_id)
        if record is None:
            return None
        rec = hub.plane.get(record.agent_id) if record.agent_id else None
        statuses = proj.run_statuses(record, hub.run_states, hub.plane)
        ws = _tasks._ws_record(hub.plane, record.agent_id)
        aggregate, _reason = _tasks._aggregate_status(record, hub.run_states, hub.plane, ws)
        status = proj.public_status(aggregate)
        phase = proj.public_phase(record, rec, statuses, aggregate)
        msgs = list(rec.messages) if rec is not None else []
        usage = rec.usage if rec is not None else None
        revs = []
        if hub.revisions is not None and record.agent_id is not None:
            try:
                revs = hub.revisions.list(record.agent_id)
            except Exception:
                revs = []
        changes = proj.changes_view(revs, ws)
        delivery = proj.delivery_view(record, ws, revs)
        terminal = None
        if aggregate == "finished":
            terminal = "finished"
        elif aggregate == "cancelled":
            terminal = "cancelled"
        elif aggregate in ("error", "expired", "delivery_failed", "failed"):
            terminal = "failed"
        error = proj._error_view(record, hub.plane, hub.run_states, aggregate)
        usage_key = None
        if isinstance(usage, dict):
            usage_key = tuple(sorted((k, v) for k, v in usage.items()))
        latest = revs[-1] if revs else None
        changes_key = (
            len(revs),
            getattr(latest, "n", 0),
            getattr(latest, "status", None),
            getattr(latest, "head_sha", None),
            (ws or {}).get("head_sha"),
        )
        delivery_key = None
        if delivery is not None:
            pr = delivery.get("pull_request") or {}
            delivery_key = (
                delivery.get("status"),
                delivery.get("branch"),
                delivery.get("pushed_head_sha"),
                pr.get("url") or pr.get("number"),
                delivery.get("error"),
            )
        return _Snap(
            status=status,
            phase=phase,
            msg_count=len(msgs),
            msg_tail=msgs,
            usage_key=usage_key,
            usage=usage,
            changes_key=changes_key,
            changes=changes,
            delivery_key=delivery_key,
            delivery=delivery,
            terminal=terminal,
            error=error,
        )

    def _diff(self, prior: _Snap, snap: _Snap) -> None:
        if (snap.status, snap.phase) != (prior.status, prior.phase):
            self.emit("session.status", {"status": snap.status, "phase": snap.phase})
        if snap.msg_tail is not None and snap.msg_count > prior.msg_count:
            for i in range(prior.msg_count, snap.msg_count):
                message = snap.msg_tail[i]
                self.emit(
                    "message.created",
                    {"message": proj._message_view(i + 1, message)},
                )
        if snap.usage_key is not None and snap.usage_key != prior.usage_key:
            self.emit("usage.updated", {"usage": snap.usage})
        if snap.changes_key is not None and snap.changes_key != prior.changes_key:
            self.emit("changes.updated", {"changes": snap.changes})
        if snap.delivery_key is not None and snap.delivery_key != prior.delivery_key:
            self.emit("delivery.updated", {"delivery": snap.delivery})
        if snap.terminal is not None and not self.terminal_sent:
            self.terminal_sent = True
            if snap.terminal == "failed":
                self.emit(
                    "session.failed",
                    {"status": "failed", "error": snap.error},
                )
            else:
                self.emit("session.completed", {"status": snap.terminal})

    def _poll_loop(self) -> None:
        # Baseline snapshot: subscribers get an immediate status event; the
        # rest of the state is a diff boundary, not history replay.
        try:
            snap = self._snapshot()
        except Exception:
            snap = None
        if snap is not None:
            self.last_snap = snap
            self.emit("session.status", {"status": snap.status, "phase": snap.phase})
            if snap.terminal is not None:
                self.terminal_sent = True
                if snap.terminal == "failed":
                    self.emit("session.failed", {"status": "failed", "error": snap.error})
                else:
                    self.emit("session.completed", {"status": snap.terminal})
        while self.live:
            if self._orphan_expired():
                break
            time.sleep(self.hub.poll_s)
            if not self.live:
                break
            try:
                snap = self._snapshot()
            except Exception:
                continue
            if snap is None:
                continue
            self._diff(self.last_snap, snap)
            self.last_snap = snap
        self._teardown()

    # -- sandbox tail -------------------------------------------------------

    def _agent_id(self) -> str | None:
        record = self.hub.task_store.get(self.session_id)
        return record.agent_id if record is not None else None

    def _tail_loop(self) -> None:
        backend = getattr(self.hub.plane, "backend", None)
        if backend is None:
            return
        while self.live:
            if self._orphan_expired():
                break
            agent_id = self._agent_id()
            rec = self.hub.plane.get(agent_id) if agent_id else None
            handle = rec.handle() if rec is not None else None
            if handle is None:
                if rec is None or rec.status in ("closed", "timed_out", "lost"):
                    return
                time.sleep(self.hub.poll_s)
                continue
            try:
                self.proc = backend.exec(
                    handle,
                    ["tail", "-n", "+1", "-F", str(handle.root / "events.jsonl")],
                    sandbox_env(handle),
                )
            except Exception:
                time.sleep(self.hub.poll_s)
                continue
            try:
                # The follow re-reads the file head on every attach; dedupe by
                # durable line number so a reattach never re-emits history.
                lineno = 0
                for line in self.proc.stdout:
                    if not self.live or self._orphan_expired():
                        break
                    lineno += 1
                    if lineno <= self.last_line or not str(line).strip():
                        continue
                    self.last_line = lineno
                    obj = _parse_raw(str(line))
                    if obj is None:
                        continue
                    for etype, data in normalize_raw(obj):
                        self.emit(etype, data)
            except Exception:
                pass
            finally:
                proc = self.proc
                self.proc = None
                try:
                    if proc is not None:
                        proc.kill()
                except Exception:
                    pass
            time.sleep(self.hub.poll_s)

    def _teardown(self) -> None:
        with self.lock:
            if not self.live:
                return
            self.live = False
            proc = self.proc
            self.proc = None
        try:
            if proc is not None:
                proc.kill()
        except Exception:
            pass
        self.hub._drop(self)


class SessionEventsHub:
    """One feed per session, shared by every SSE subscriber."""

    def __init__(
        self,
        *,
        plane: Any,
        task_store: Any,
        run_states: Any,
        revisions: Any = None,
        poll_s: float = 0.5,
    ) -> None:
        self.plane = plane
        self.task_store = task_store
        self.run_states = run_states
        self.revisions = revisions
        self.poll_s = poll_s
        self._feeds: dict[str, _Feed] = {}
        self._lock = threading.Lock()

    def feed(self, session_id: str) -> _Feed:
        with self._lock:
            feed = self._feeds.get(session_id)
            if feed is None or not feed.live:
                feed = _Feed(self, session_id)
                self._feeds[session_id] = feed
                feed.start()
            return feed

    def emit_for(self, session_id: str, etype: str, data: dict[str, Any]) -> None:
        """Side-band injection for mutation outcomes (e.g. message refusal)."""
        with self._lock:
            feed = self._feeds.get(session_id)
        if feed is not None:
            feed.emit(etype, data)

    def _drop(self, feed: _Feed) -> None:
        with self._lock:
            if self._feeds.get(feed.session_id) is feed:
                self._feeds.pop(feed.session_id, None)


def stream_events(feed: _Feed, last_event_id: str | None) -> Iterator[str]:
    """Yield SSE frames for a subscription: buffer replay, then live."""
    try:
        last_seq = int(last_event_id) if last_event_id else None
    except ValueError:
        last_seq = None
    # A last_id beyond the feed's own sequence is stale (hub restart): fall
    # back to the full retained window rather than silently skipping events.
    if last_seq is not None and last_seq > feed.seq:
        last_seq = None
    q = feed.subscribe(last_seq)
    yield ": keepalive\n\n"
    try:
        for event in feed.replay(last_seq):
            yield event.sse()
        while feed.live:
            try:
                event = q.get(timeout=0.25)
            except queue_mod.Empty:
                continue
            yield event.sse()
    finally:
        feed.unsubscribe(q)
