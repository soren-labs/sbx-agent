"""Process supervisor for one official CLI Turn at a time.

Acceptance is journaled before launch; the pid is journaled right after spawn.
Terminal evidence is always spooled (reserved headroom) with the final local
watermark. The supervisor never relaunches an ambiguous operation.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from runtime.daemon.journal import Journal, SpoolPressure
from runtime.daemon.process_anchor import scope_name
from runtime.harnesses.protocol import Harness, NativeInvocation, PreparedHarness, TurnContext
from runtime.security.redaction import Redactor

STOP_GRACE_SECONDS = 5.0
_PROCESS_SCOPE = "SBX_MANAGED_PROCESS_SCOPE"
_scopes: dict[int, str] = {}


class ManagedProcess:
    """Popen streams/pid belong to the anchor; poll/wait report the command exit."""

    def __init__(self, anchor: subprocess.Popen[bytes], status_fd: int) -> None:
        self.anchor = anchor
        self.status_fd = status_fd
        self.returncode: int | None = None
        self.buffer = b""
        self.lock = threading.Lock()
        os.set_blocking(status_fd, False)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.anchor, name)

    def poll(self) -> int | None:
        with self.lock:
            anchor_code = self.anchor.poll()  # reap the anchor once it has finished
            if self.returncode is None:
                try:
                    self.buffer += os.read(self.status_fd, 128)
                except BlockingIOError:
                    pass
                if b"\n" in self.buffer:
                    self.returncode = int(self.buffer.split(b"\n", 1)[0])
                elif anchor_code is not None:
                    self.returncode = anchor_code
                if self.returncode is not None:
                    os.close(self.status_fd)
            return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while (code := self.poll()) is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.anchor.args, timeout)
            time.sleep(0.01)
        return code


def managed_popen(
    argv: list[str], *, env: dict[str, str], scope: str | None = None, **kwargs: Any
) -> ManagedProcess:
    """Retain kernel ancestry independently of descendant environments/dumpability."""
    scope = scope or uuid.uuid4().hex
    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("process_anchor.py")),
                scope,
                str(write_fd),
                *argv,
            ],
            env={**env, _PROCESS_SCOPE: scope},
            pass_fds=(write_fd,),
            **kwargs,
        )
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    # The anchor establishes subreaping before it launches any untrusted command.
    launched = b""
    while not launched.endswith(b"\n"):
        part = os.read(read_fd, 1)
        if not part:
            break
        launched += part
    status = json.loads(launched or b'{"errno": 5}')
    if status:
        os.close(read_fd)
        proc.wait()
        raise OSError(status["errno"], "managed command launch failed")
    _scopes[proc.pid] = scope
    return ManagedProcess(proc, read_fd)


def kill_group(
    pid: int,
    grace: float = STOP_GRACE_SECONDS,
    *,
    scope: str | None = None,
    require_presence: bool = False,
) -> bool:
    """Stop the managed process group and its session's job-control groups.

    A reaped leader is not proof that its descendants have stopped writing.
    """
    scope = scope or _scopes.get(pid)
    try:
        # Before startup registration, an empty scan cannot distinguish a dead
        # launch from an anchor suspended before assigning its scope name.
        if require_presence and not _live_groups(pid, scope=scope):
            return False
        for sig, timeout in ((signal.SIGTERM, grace), (signal.SIGKILL, 2.0)):
            # The anchor is in a separate session and exits only after reaping
            # every descendant. Never kill it and lose ancestry during escalation.
            for group in _live_groups(pid, scope=scope, signal_targets=True):
                try:
                    os.killpg(group, sig)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not _live_groups(pid, scope=scope):
                    if pid:
                        try:
                            os.waitpid(pid, os.WNOHANG)
                        except ChildProcessError:
                            pass  # after a daemon restart, the anchor is not our child
                    _scopes.pop(pid, None)
                    return True
                time.sleep(0.05)
        return False
    except OSError:
        return False  # inability to inspect or signal cannot confirm stop


def _live_groups(pid: int, *, scope: str | None = None, signal_targets: bool = False) -> set[int]:
    """Live members of a managed session, excluding zombies that cannot write."""
    scope = scope or _scopes.get(pid)
    name = scope_name(scope) if scope else None
    processes = {}
    anchors = set()
    owned = {pid} if pid else set()
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            comm, tail = stat.read_text().rsplit(")", 1)
            fields = tail.split()
            member = int(stat.parent.name)
            parent, group, session = map(int, fields[1:4])
            processes[member] = (parent, group, fields[0])
            if name and comm.split("(", 1)[1] == name and stat.stat().st_uid == os.getuid():
                anchors.add(member)
            if (pid and (group == pid or session == pid)) or (
                name and comm.split("(", 1)[1] == name and stat.stat().st_uid == os.getuid()
            ):
                owned.add(member)
        except (FileNotFoundError, ProcessLookupError):
            continue  # confirmed disappearance during the scan
    while True:
        descendants = {p for p, (parent, _, _) in processes.items() if parent in owned}
        if descendants <= owned:
            break
        owned |= descendants
    return {
        group
        for p, (_, group, state) in processes.items()
        if p in owned and state != "Z" and not (signal_targets and group in anchors)
    }


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie child of ours counts as gone
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split()[2] != "Z"
    except OSError:
        return True


class TurnRun(threading.Thread):
    def __init__(
        self,
        journal: Journal,
        operation_id: str,
        harness: Harness,
        context: TurnContext,
        prepared: PreparedHarness,
        invocation: NativeInvocation,
    ) -> None:
        super().__init__(daemon=True, name=f"turn-{operation_id}")
        self.journal = journal
        self.operation_id = operation_id
        self.harness = harness
        self.context = context
        self.prepared = prepared
        self.invocation = invocation
        self.redactor = Redactor(prepared.secrets)
        self.state = harness.new_state(context)
        self.proc: ManagedProcess | None = None
        self.cancel_requested = False
        self.timed_out = False
        self.pressure = False
        self.stop_confirmed: bool | None = None
        self.stderr_tail = ""
        self.done = threading.Event()

    @property
    def execution_id(self) -> str:
        return self.context.execution_id

    def _spool(self, type: str, payload: dict[str, Any], *, terminal: bool = False) -> int:
        return self.journal.append(
            self.execution_id, type, self.redactor.value(payload), terminal=terminal
        )

    def run(self) -> None:
        manifest = self.harness.describe()
        try:
            self.journal.op_update(self.operation_id, status="starting")
            self.proc = managed_popen(
                self.invocation.argv,
                cwd=str(self.invocation.cwd),
                env=self.invocation.env,
                scope=self.operation_id,
                stdin=subprocess.DEVNULL if self.invocation.stdin is None else subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            self._finalize(exit_code=None, spawn_error=f"spawn failed: {exc.strerror}")
            return
        self.journal.op_update(self.operation_id, status="started", pid=self.proc.pid)
        self._spool(
            "execution.started",
            {
                "provider_id": manifest.provider_id,
                "cli_version": manifest.cli_version,
                "adapter_version": manifest.adapter_version,
                "resumed": bool(self.context.native_binding),
            },
        )
        stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        stderr_thread.start()
        # Wait independently of stdout: descendants can keep inherited pipes
        # open after the parent exits. Stop them before publishing completion.
        exit_thread = threading.Thread(target=self._wait_for_exit, daemon=True)
        exit_thread.start()
        timer = threading.Timer(self.context.deadline_seconds, self._deadline)
        timer.daemon = True
        timer.start()
        try:
            assert self.proc.stdout is not None
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace")
                for observation in self.harness.normalize(line, self.state):
                    try:
                        self._spool(observation["type"], observation["payload"])
                    except SpoolPressure:
                        # Backpressure: stop intake by stopping the CLI, never drop evidence.
                        self.pressure = True
                        self.stop_confirmed = kill_group(self.proc.pid)
                        break
                if self.pressure:
                    break
            exit_code = self.proc.wait()
        finally:
            timer.cancel()
        exit_thread.join()
        stderr_thread.join(timeout=2)
        killed = self.cancel_requested or self.timed_out or self.pressure
        self._finalize(exit_code=None if (killed and exit_code < 0) else exit_code)

    def _wait_for_exit(self) -> None:
        assert self.proc is not None
        self.proc.wait()
        self.stop_confirmed = kill_group(self.proc.pid)

    def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for raw in self.proc.stderr:
            self.stderr_tail = (self.stderr_tail + raw.decode("utf-8", "replace"))[-8000:]

    def _deadline(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.timed_out = True
            self.stop_confirmed = kill_group(self.proc.pid)

    def stop(self) -> bool:
        self.cancel_requested = True
        if self.proc is None:
            return True
        self.stop_confirmed = kill_group(self.proc.pid)
        return self.stop_confirmed

    def _finalize(self, *, exit_code: int | None, spawn_error: str | None = None) -> None:
        if self.journal.op_get(self.operation_id)["status"] == "lost":
            self.harness.release(self.prepared)
            self.done.set()
            return
        for observation in self.harness.finish(self.state):
            self._spool(observation["type"], observation["payload"], terminal=True)
        evidence = {
            "exit_code": exit_code,
            "stderr_tail": self.redactor.text(self.stderr_tail),
            "cancelled": self.cancel_requested,
            "timed_out": self.timed_out,
            "spool_pressure": self.pressure,
        }
        outcome = self.harness.classify_outcome(evidence, self.state)
        if spawn_error:
            outcome.verdict, outcome.error_code, outcome.message = (
                "failure",
                "spawn_failed",
                spawn_error,
            )
        if self.pressure:
            outcome.verdict, outcome.error_code, outcome.message = (
                "unknown",
                "spool_pressure",
                "evidence backpressure stopped the CLI",
            )
        if self.stop_confirmed is False:
            outcome.verdict, outcome.error_code, outcome.message = (
                "unknown",
                "outcome_unknown",
                "CLI descendants have not confirmed stop",
            )
        scrub_failures = self.harness.release(self.prepared)
        self._spool(
            "execution.observed_terminal",
            {
                "verdict": outcome.verdict,
                "error_code": outcome.error_code,
                "message": self.redactor.text(outcome.message)[:500],
                "credential_health": outcome.credential_health,
                "retry_advice": outcome.retry_advice,
                "exit_code": exit_code,
                "native_id": self.state.get("native_id"),
                "cancelled": self.cancel_requested,
                "timed_out": self.timed_out,
                "scrub_failures": scrub_failures,
            },
            terminal=True,
        )
        stopped = self.proc is None or self.stop_confirmed is True
        final = self.journal.last_seq() + 1
        seq = self._spool(
            "execution.stopped",
            {"stopped": stopped, "final_local_seq": final, "cancelled": self.cancel_requested},
            terminal=True,
        )
        result = {
            "verdict": outcome.verdict,
            "exit_code": exit_code,
            "final_local_seq": seq,
            "native_id": self.state.get("native_id"),
        }
        self.journal.op_update(
            self.operation_id,
            status="succeeded" if outcome.verdict == "success" else "failed",
            result=result,
        )
        self.done.set()
