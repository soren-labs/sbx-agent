"""Daemon application loop — handshake, op dispatch, spool pump (RFC 167 §03)."""

from __future__ import annotations

import platform
import threading
import time
from pathlib import Path

from protocol.runtime import (
    FRAME_EVENTS_ACK,
    FRAME_HELLO_ACCEPTED,
    FRAME_HELLO_REJECTED,
    FRAME_LEASE_REVOKED,
    FRAME_OPERATION_SUBMIT,
    FRAME_PING,
    FRAME_PONG,
    OperationEnvelope,
    OperationKind,
    hello_frame,
    make_frame,
)

from .journal import Journal
from .operations import OperationError, Operations
from .supervisor import Supervisor
from .transport import Transport, TransportClosed, connect

EVENT_BATCH_LIMIT = 128
SPOOL_FLUSH_INTERVAL_S = 0.05


class DaemonApp:
    def __init__(
        self,
        *,
        state_dir: Path,
        worktree_root: Path,
        lease_id: str,
        lease_generation: int,
        enrollment_token: str,
        grant_expires_at: float | None,
        endpoint: str,
        image_digest: str | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.worktree_root = Path(worktree_root)
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        (self.worktree_root / ".sbx").mkdir(exist_ok=True)
        self.lease_id = lease_id
        self.lease_generation = lease_generation
        self.enrollment_token = enrollment_token
        self.endpoint = endpoint
        self.image_digest = image_digest
        self.journal = Journal(self.state_dir / "journal.db")
        self.supervisor = Supervisor(
            spool_append=self._spool,
            on_terminal=lambda proc, obs: self._terminal(proc, obs),
            normalize=lambda line, state: self._normalize(line, state),
        )
        self.operations = Operations(
            journal=self.journal,
            supervisor=self.supervisor,
            worktree_root=self.worktree_root,
            state_root=self.state_dir,
            lease_id=lease_id,
            lease_generation=lease_generation,
            grant_expires_at=grant_expires_at,
            emit=self._send,
        )
        self._transport: Transport | None = None
        self._send_lock = threading.Lock()
        self._connected = threading.Event()
        self._stopping = threading.Event()
        self._revoked = threading.Event()
        self._normalize_impl = None

    # -- wiring ---------------------------------------------------------

    def _normalize(self, line: str, state):
        # Fallback normalize when the operation did not pin a provider —
        # currently only OpenCode is registered.
        from runtime.harnesses.registry import get_harness

        return get_harness("opencode", self.state_dir).normalize(line, state)

    def _spool(self, kind: str, payload: dict) -> int:
        seq = self.journal.spool_append(kind, payload)
        return seq

    def _terminal(self, proc, observations) -> None:
        self.operations.on_process_terminal(proc, observations)

    def _send(self, frame: dict) -> None:
        transport = self._transport
        if transport is None:
            return
        with self._send_lock:
            try:
                transport.send(frame)
            except OSError:
                pass

    # -- lifecycle -------------------------------------------------------

    def run(self) -> int:
        """Connect → hello → frame loop until shutdown/revoked."""
        from runtime.harnesses.registry import manifests

        while not self._stopping.is_set():
            try:
                self._transport = connect(self.endpoint, timeout=15.0)
            except OSError:
                if self._stopping.wait(0.5):
                    break
                continue
            hello = hello_frame(
                lease_id=self.lease_id,
                lease_generation=self.lease_generation,
                runtime_epoch=self.journal.runtime_epoch,
                token=self.enrollment_token,
                image_digest=self.image_digest,
                runtime_build=f"python/{platform.python_version()}",
                harness_manifests=manifests(self.state_dir),
                recovered_operations=self.journal.all_operation_ids(),
                spool_watermark=self.journal.committed_watermark(),
                health={"status": "ok"},
            )
            self._send(hello)
            try:
                frame = self._transport.recv()
            except TransportClosed:
                time.sleep(0.2)
                continue
            if frame.get("frame") == FRAME_HELLO_REJECTED:
                self._revoked.set()
                break
            if frame.get("frame") != FRAME_HELLO_ACCEPTED:
                time.sleep(0.2)
                continue
            self._connected.set()
            self._loop()
            self._connected.clear()
            self._transport = None
            if self._revoked.is_set() or self.operations._shutdown.is_set():
                break
            time.sleep(0.2)  # reconnect backoff
        self.journal.close()
        return 0

    def _loop(self) -> None:
        flusher = threading.Thread(target=self._spool_pump, daemon=True)
        flusher.start()
        try:
            while not self._stopping.is_set() and not self.operations._shutdown.is_set():
                try:
                    frame = self._transport.recv()
                except (TransportClosed, OSError):
                    break
                self._handle(frame)
        finally:
            self._stopping.wait(0.1)

    def _handle(self, frame: dict) -> None:
        kind = frame.get("frame")
        if kind == FRAME_PING:
            self._send(make_frame(FRAME_PONG))
            return
        if kind == FRAME_EVENTS_ACK:
            self.journal.spool_ack(int(frame.get("committed_local_seq") or 0))
            return
        if kind == FRAME_LEASE_REVOKED:
            self.supervisor.stop_all()
            self._revoked.set()
            self._stopping.set()
            return
        if kind == FRAME_OPERATION_SUBMIT:
            self._on_operation_submit(frame)
            return

    def _on_operation_submit(self, frame: dict) -> None:
        try:
            env = OperationEnvelope.from_frame(frame)
        except Exception as exc:
            self._send(
                make_frame(
                    "operation.rejected",
                    operation_id=str(frame.get("operation_id") or ""),
                    error={"code": "payload_invalid", "message": str(exc)},
                )
            )
            return
        try:
            status, prior = self.operations.accept(env)
        except OperationError as exc:
            self._send(
                make_frame(
                    "operation.rejected",
                    operation_id=env.operation_id,
                    error=exc.error.to_dict(),
                )
            )
            return
        if status == "conflict":
            self._send(
                make_frame(
                    "operation.rejected",
                    operation_id=env.operation_id,
                    error={
                        "code": "operation_conflict",
                        "message": "same operation_id with different body",
                    },
                )
            )
            return
        if status == "replay":
            if prior and prior.get("state") in (
                "succeeded",
                "failed",
                "interrupted",
                "unknown",
            ):
                self._send(
                    make_frame(
                        "operation.result",
                        operation_id=env.operation_id,
                        state=prior["state"],
                        result=prior.get("result") or {},
                        replay=True,
                        final_local_seq=self.journal.spool_max_seq(),
                    )
                )
            else:
                self._send(
                    make_frame(
                        "operation.accepted",
                        operation_id=env.operation_id,
                        state=prior["state"] if prior else "accepted",
                        replay=True,
                    )
                )
            return
        self._send(
            make_frame("operation.accepted", operation_id=env.operation_id, state="accepted")
        )

        def execute() -> None:
            try:
                result = self.operations.run(env)
            except OperationError as exc:
                self.operations.settle_terminal(
                    env.operation_id, "failed", {"error": exc.error.to_dict()}
                )
            except Exception as exc:  # noqa: BLE001 — daemon must survive
                self.operations.settle_terminal(
                    env.operation_id,
                    "failed",
                    {"error": {"code": "process_error", "message": str(exc)}},
                )
            else:
                # Turn spawns are async — their terminal verdict is emitted by
                # on_process_terminal. Every other operation settles here so
                # submit_for_result callers receive operation.result.
                if env.operation_kind not in (
                    OperationKind.TURN_START,
                    OperationKind.TURN_RESUME,
                ):
                    self.operations.settle_terminal(env.operation_id, "succeeded", result or {})

        threading.Thread(target=execute, daemon=True).start()

    def _spool_pump(self) -> None:
        while not self._stopping.is_set() and not self.operations._shutdown.is_set():
            rows = self.journal.spool_uncommitted(EVENT_BATCH_LIMIT)
            if rows:
                self._send(
                    make_frame(
                        "events.batch",
                        runtime_epoch=self.journal.runtime_epoch,
                        from_seq=rows[0][0],
                        to_seq=rows[-1][0],
                        events=[
                            {"local_seq": seq, "kind": kind, "payload": payload}
                            for seq, kind, payload in rows
                        ],
                    )
                )
            time.sleep(SPOOL_FLUSH_INTERVAL_S)

    def stop(self) -> None:
        self._stopping.set()
        self.supervisor.stop_all()
        if self._transport is not None:
            self._transport.close()
