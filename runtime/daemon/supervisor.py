"""Process supervision (RFC 167 §03).

Supervised process groups, bounded TERM→KILL escalation, stdout line pump
→ normalize → spool. Terminal evidence (process.exited + outcome) persists
in the journal/spool after stop. The supervisor never relaunches an
ambiguously accepted Execution.
"""

from __future__ import annotations

import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from protocol.events import Observation, ObservationKind

from runtime.harnesses.protocol import NativeInvocation, NormalizeState

_SIGTERM_GRACE_S = 5.0
_STDERR_TAIL_LIMIT = 16 * 1024


def kill_process_group(pid: int, sig: int) -> None:
    try:
        import os

        os.killpg(pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            import os

            os.kill(pid, sig)
        except ProcessLookupError:
            return


@dataclass
class SupervisedProcess:
    execution_id: str
    operation_id: str
    invocation: NativeInvocation
    normalize: Callable[[str, NormalizeState], list[Observation]] | None = None
    popen: subprocess.Popen | None = None
    started_at: float = 0.0
    cancel_requested: bool = False
    exit_code: int | None = None
    signal_number: int | None = None
    stderr_tail: str = ""
    bad_frames: int = 0
    observations: list[Observation] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)


class Supervisor:
    """Runs one mutating CLI at a time per the caller's scheduling."""

    def __init__(
        self,
        spool_append: Callable[[str, dict], int],
        on_terminal: Callable[[SupervisedProcess, list[Observation]], None],
        normalize: Callable[[str, NormalizeState], list[Observation]],
    ) -> None:
        self._spool_append = spool_append
        self._on_terminal = on_terminal
        self._normalize = normalize
        self._lock = threading.Lock()
        self._procs: dict[str, SupervisedProcess] = {}
        self.quiesced = threading.Event()  # set = no mutating intake

    def launch(self, proc: SupervisedProcess) -> None:
        """Spawn the invocation; must only be called AFTER durable accept."""
        if self.quiesced.is_set():
            raise RuntimeError("runtime quiesced: refusing to launch")
        invocation = proc.invocation
        stdin = subprocess.DEVNULL if invocation.stdin == "devnull" else subprocess.PIPE
        popen = subprocess.Popen(
            list(invocation.argv),
            cwd=str(invocation.cwd),
            env=dict(invocation.env),
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        proc.popen = popen
        proc.started_at = time.time()
        with self._lock:
            self._procs[proc.operation_id] = proc

        def read_stderr() -> None:
            assert popen.stderr is not None
            tail = popen.stderr.read()
            proc.stderr_tail = (tail or "")[-_STDERR_TAIL_LIMIT:]

        def pump() -> None:
            state = NormalizeState()
            expected = proc.invocation.env.get("SBX_EXPECTED_NATIVE_ID")
            if expected:
                state.data["expected_native_id"] = expected
            normalize = proc.normalize or self._normalize
            assert popen.stdout is not None
            try:
                for line in popen.stdout:
                    observations = normalize(line, state)
                    if not observations:
                        stripped = line.strip()
                        if stripped and not stripped.startswith("{"):
                            # No JSON object at all — a malformed frame per
                            # the transport contract (distinct from
                            # parseable-but-unknown kinds, which normalize
                            # to NOOP observations).
                            proc.bad_frames += 1
                            self._emit(
                                Observation(
                                    kind=ObservationKind.DIAGNOSTIC,
                                    payload={"bad_frame": True, "raw_tail": line[:512]},
                                    observed_at=time.time(),
                                ),
                                proc,
                            )
                        continue
                    for obs in observations:
                        self._emit(obs, proc)
            finally:
                code = popen.wait()
                duration_ms = int((time.time() - proc.started_at) * 1000)
                if code is not None and code < 0:
                    proc.signal_number = -code
                    proc.exit_code = None
                else:
                    proc.exit_code = code
                exit_obs = Observation(
                    kind=ObservationKind.PROCESS_EXITED,
                    process={
                        "exit_code": proc.exit_code,
                        "signal": proc.signal_number,
                        "duration_ms": duration_ms,
                        "cancel_requested": proc.cancel_requested,
                    },
                    observed_at=time.time(),
                )
                self._emit(exit_obs, proc)
                proc.done.set()
                with self._lock:
                    self._procs.pop(proc.operation_id, None)
                self._on_terminal(proc, proc.observations)

        threading.Thread(target=read_stderr, daemon=True).start()
        threading.Thread(target=pump, daemon=True).start()

    def cancel(self, operation_id: str) -> bool:
        """SIGTERM escalation. Signal delivery is not confirmed cancellation —
        terminal evidence arrives via PROCESS_EXITED."""
        with self._lock:
            proc = self._procs.get(operation_id)
        if proc is None or proc.popen is None or proc.popen.poll() is not None:
            return False
        proc.cancel_requested = True
        pid = proc.popen.pid
        kill_process_group(pid, signal.SIGTERM)

        def escalate() -> None:
            try:
                proc.done.wait(_SIGTERM_GRACE_S)
                if not proc.done.is_set():
                    kill_process_group(pid, signal.SIGKILL)
            except Exception:
                pass

        threading.Thread(target=escalate, daemon=True).start()
        return True

    def stop_all(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            self.cancel(proc.operation_id)

    def active_operations(self) -> list[str]:
        with self._lock:
            return list(self._procs)

    def _emit(self, obs: Observation, proc: SupervisedProcess) -> None:
        proc.observations.append(obs)
        self._spool_append("observation", obs.to_dict())
