"""Ingress server — accepts daemon-outbound connections (RFC 167 §03).

One asyncio loop thread per process owns every live runtime channel:
``hello`` (enrollment verify) → per-lease registration → operation frames
both directions → ``events.batch`` → committed ack only after the ingest
callback commits to the DB.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from protocol.runtime import (
    FRAME,
    FRAME_EVENTS_ACK,
    FRAME_HELLO,
    FRAME_HELLO_ACCEPTED,
    FRAME_HELLO_REJECTED,
    FRAME_OPERATION_RESULT,
    FRAME_OPERATION_SUBMIT,
    FRAME_PING,
    FRAME_PONG,
    PROTOCOL_MAJOR,
    decode_frame,
    encode_frame,
    make_frame,
)

#: verify(hello: dict) -> dict|None — returns the lease row (with handle) on
#: success, None to reject.
HelloVerifier = Callable[[dict], dict | None]
#: on_events(lease_row, epoch, events) -> int — committed local_seq watermark.
EventsHandler = Callable[[dict, str, list[dict]], int]
#: on_result(lease_row, frame) -> None
ResultHandler = Callable[[dict, dict], None]
#: on_detach(lease_row, epoch) -> None — transport lost (NOT a domain verdict).
DetachHandler = Callable[[dict, str], None]


@dataclass
class _Channel:
    lease_row: dict
    runtime_epoch: str
    hello: dict
    writer: asyncio.StreamWriter
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    connected_at: float = field(default_factory=time.time)


class IngressServer:
    def __init__(
        self,
        *,
        verify_hello: HelloVerifier,
        on_events: EventsHandler,
        on_result: ResultHandler,
        on_detach: DetachHandler,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._verify = verify_hello
        self._on_events = on_events
        self._on_result = on_result
        self._on_detach = on_detach
        self._host = host
        self._port = port
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None
        self._thread: threading.Thread | None = None
        self._channels: dict[str, _Channel] = {}  # lease_id -> channel
        self._lock = threading.Lock()
        self._attach_events: dict[str, threading.Event] = {}

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        ready = threading.Event()

        def run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._server = self._loop.run_until_complete(self._serve())
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, name="sbx-ingress", daemon=True)
        self._thread.start()
        ready.wait(5)

    async def _serve(self) -> asyncio.AbstractServer:
        return await asyncio.start_server(self._accept, self._host, self._port)

    @property
    def endpoint(self) -> str:
        sock = self._server.sockets[0]
        host, port = sock.getsockname()[:2]
        return f"tcp://{host}:{port}"

    def stop(self) -> None:
        if self._loop is None:
            return

        def _shutdown() -> None:
            for channel in list(self._channels.values()):
                try:
                    channel.writer.close()
                except Exception:
                    pass
            for task in asyncio.all_tasks(self._loop):
                if task is not asyncio.current_task():
                    task.cancel()

        self._loop.call_soon_threadsafe(_shutdown)
        # Let cancellations run through before stopping the loop.
        deadline = time.time() + 3
        while time.time() < deadline:
            pending = asyncio.all_tasks(self._loop) if self._loop.is_running() else set()
            pending = {t for t in pending if not t.done()}
            if not pending:
                break
            time.sleep(0.05)
        if self._server is not None:
            self._loop.call_soon_threadsafe(self._server.close)
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    # -- connection handling ---------------------------------------------

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        asyncio.ensure_future(self._session(reader, writer))

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        channel: _Channel | None = None
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=15)
            hello = decode_frame(line)
            if hello.get(FRAME) != FRAME_HELLO:
                writer.close()
                return
            lease_row = self._verify(hello)
            if lease_row is None:
                self._send(
                    writer,
                    make_frame(
                        FRAME_HELLO_REJECTED,
                        error={"code": "enrollment_invalid", "message": "enrollment rejected"},
                    ),
                )
                writer.close()
                return
            protocol = hello.get("protocol") or {}
            if int(protocol.get("major") or -1) != PROTOCOL_MAJOR:
                self._send(
                    writer,
                    make_frame(
                        FRAME_HELLO_REJECTED,
                        error={
                            "code": "protocol_incompatible",
                            "message": "protocol major mismatch",
                        },
                    ),
                )
                writer.close()
                return
            channel = _Channel(
                lease_row=lease_row,
                runtime_epoch=str(hello.get("runtime_epoch") or ""),
                hello=hello,
                writer=writer,
            )
            lease_id = lease_row["id"]
            with self._lock:
                old = self._channels.get(lease_id)
                if old is not None and old is not channel:
                    old.writer.close()
                self._channels[lease_id] = channel
                ev = self._attach_events.setdefault(lease_id, threading.Event())
                ev.set()
            self._send(
                writer,
                make_frame(
                    FRAME_HELLO_ACCEPTED,
                    protocol={"major": PROTOCOL_MAJOR, "minor": 0},
                    lease_generation=lease_row["generation"],
                ),
            )
            await self._read_loop(channel, reader)
        except (TimeoutError, ValueError, KeyError):
            pass
        finally:
            if channel is not None:
                lease_id = channel.lease_row["id"]
                with self._lock:
                    if self._channels.get(lease_id) is channel:
                        self._channels.pop(lease_id, None)
                for future in channel.pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError("runtime detached"))
                # Only report detach when this channel is still the lease's
                # current channel — a stale closing channel must not mark a
                # reconnected lease lost.
                with self._lock:
                    current = self._channels.get(lease_id)
                if current is None or current is channel:
                    try:
                        self._on_detach(channel.lease_row, channel.runtime_epoch)
                    except Exception:
                        pass
            writer.close()

    async def _read_loop(self, channel: _Channel, reader: asyncio.StreamReader) -> None:
        while True:
            line = await reader.readline()
            if not line:
                return
            try:
                frame = decode_frame(line)
            except ValueError:
                continue
            kind = frame.get(FRAME)
            if kind == FRAME_PING:
                self._send(channel.writer, make_frame(FRAME_PONG))
            elif kind == "events.batch":
                committed = self._on_events(
                    channel.lease_row,
                    str(frame.get("runtime_epoch") or channel.runtime_epoch),
                    list(frame.get("events") or []),
                )
                self._send(
                    channel.writer,
                    make_frame(FRAME_EVENTS_ACK, committed_local_seq=committed),
                )
            elif kind == FRAME_OPERATION_RESULT:
                op_id = str(frame.get("operation_id") or "")
                future = channel.pending.pop(op_id, None)
                if future is not None and not future.done():
                    future.set_result(frame)
                try:
                    self._on_result(channel.lease_row, frame)
                except Exception:
                    pass
            elif kind == "operation.accepted" or kind == "operation.rejected":
                future = channel.pending.get(str(frame.get("operation_id") or ""))
                if future is None or future.done():
                    pass
                elif getattr(future, "sbx_await_result", False) and kind == "operation.accepted":
                    pass  # keep waiting for the terminal operation.result frame
                else:
                    future.set_result(frame)
            elif kind == "health.report":
                pass  # evidence only; no domain verdict

    @staticmethod
    def _send(writer: asyncio.StreamWriter, frame: dict) -> None:
        writer.write(encode_frame(frame))

    # -- client-facing API -------------------------------------------------

    def attach(self, lease_id: str, timeout: float = 30.0) -> _Channel | None:
        """Block until a daemon for ``lease_id`` has completed hello."""
        with self._lock:
            ev = self._attach_events.setdefault(lease_id, threading.Event())
        if ev.wait(timeout):
            return self._channels.get(lease_id)
        return None

    def attached(self, lease_id: str) -> bool:
        return lease_id in self._channels

    def submit_operation(
        self,
        lease_id: str,
        envelope: dict,
        timeout: float = 30.0,
        await_result: bool = False,
    ) -> dict:
        """Send operation.submit; block for accepted/rejected — or, with
        ``await_result``, for the terminal operation.result frame (control
        ops like changes.capture / files.read whose payload IS the verdict)."""
        channel = self._channels.get(lease_id)
        if channel is None:
            raise ConnectionError(f"no runtime channel for lease {lease_id}")
        op_id = str(envelope["operation_id"])

        async def go() -> dict:
            future = asyncio.get_event_loop().create_future()
            future.sbx_await_result = await_result  # read_loop honors this
            channel.pending[op_id] = future
            self._send(channel.writer, make_frame(FRAME_OPERATION_SUBMIT, **envelope))
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            finally:
                channel.pending.pop(op_id, None)

        coro = asyncio.run_coroutine_threadsafe(go(), self._loop)
        return coro.result(timeout + 5)

    def revoke(self, lease_id: str) -> None:
        channel = self._channels.pop(lease_id, None)
        if channel is not None:
            try:
                self._send(
                    channel.writer,
                    make_frame("lease.revoked", lease_id=lease_id),
                )
            except Exception:
                pass
            channel.writer.close()

    def channels(self) -> list[str]:
        return list(self._channels)
