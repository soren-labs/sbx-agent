import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path

from protocol.runtime import ProtocolError

from runtime.harnesses.protocol import TurnContext
from runtime.harnesses.registry import get_harness
from runtime.security.redaction import Redactor


class Supervisor:
    def __init__(self, journal, worktree: Path, home: Path, harness_factory=get_harness):
        self.journal, self.worktree, self.home = journal, worktree, home
        self.harness_factory = harness_factory
        self.lock = threading.RLock()
        self.process = None
        self.active_operation = None
        self.cancelled = set()
        self.known_secrets = set()

    def stop(self, operation):
        with self.lock:
            self.cancelled.add(operation)
            if self.active_operation == operation and self.process:
                self.kill_group(self.process)
            row = self.journal.get(operation)
            if row and row["state"] == "accepted":
                self.journal.update(
                    operation, "terminal", result={"outcome": "cancelled", "stopped": True}
                )

    @staticmethod
    def kill_group(process):
        try:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        except ProcessLookupError:
            pass
        # Descendants in this group must not keep mutating files after CLI exit.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def run_turn(self, frame):
        operation = frame.operation_id
        payload = frame.payload
        credentials = payload.get("credential_bundle", {})
        redactor = Redactor(credentials.values())
        self.known_secrets.update(redactor.values)
        state = {"expected_native_id": payload.get("native_id")}
        context = TurnContext(
            frame.session_id,
            payload["turn_id"],
            payload["execution_id"],
            frame.lease_generation,
            int(self.journal.metadata("generation")),
            self.worktree,
            self.home,
            payload.get("model", ""),
            payload["prompt"],
            payload.get("native_id"),
            min(float(payload.get("timeout", 300)), max(0, frame.grant_expires_at - time.time())),
            payload.get("settings", {}),
        )
        harness = self.harness_factory(payload.get("provider_id", "opencode"))
        process = None
        try:
            with self.lock:
                if self.active_operation not in {None, operation}:
                    raise ProtocolError("waiting_capacity")
                if (
                    operation in self.cancelled
                    or self.journal.get(operation)["state"] != "accepted"
                ):
                    if self.active_operation == operation:
                        self.active_operation, self.process = None, None
                    return
                self.active_operation = operation
                # Durable starting before spawn. Repeated accepted/starting never relaunches.
                self.journal.update(operation, "starting")
                env = harness.prepare(context, credentials)
                invocation = (
                    harness.resume_turn(context, env)
                    if context.native_id
                    else harness.start_turn(context, env)
                )
                process = subprocess.Popen(
                    invocation.argv,
                    cwd=invocation.cwd,
                    env=invocation.env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    text=True,
                )
                self.process = process
                self.journal.update(operation, "started", pid=process.pid)
                self.journal.append(operation, "execution.started", {})
            stderr_tail = []

            def drain_stderr():
                for line in iter(lambda: process.stderr.readline(2001), ""):
                    stderr_tail.append(redactor.clean(line[-2000:]))
                    if len(stderr_tail) > 16:
                        del stderr_tail[0]

            threading.Thread(target=drain_stderr, daemon=True).start()
            timer = threading.Timer(context.deadline, lambda: self.stop(operation))
            timer.start()
            try:
                for raw in iter(lambda: process.stdout.readline(1_000_001), ""):
                    if len(raw) > 1_000_000:
                        raise ProtocolError("malformed_frame")
                    try:
                        frame_data = json.loads(raw)
                    except ValueError:
                        state["error"] = "malformed_frame"
                        continue
                    for observation in harness.normalize(frame_data, state):
                        self.journal.append(
                            operation, observation["type"], redactor.clean(observation["payload"])
                        )
                exit_code = process.wait(timeout=5)
            finally:
                timer.cancel()
                self.kill_group(process)
            evidence = {"exit_code": exit_code, "stopped": True}
            outcome = harness.classify_outcome(evidence, state)
            result = {
                "outcome": "cancelled" if operation in self.cancelled else outcome.verdict,
                "stopped": True,
                "evidence": evidence,
                "harness": asdict(outcome),
                "native_id": outcome.native_id,
                "text": "\n".join(state.get("parts", {}).values()),
                "result_text": next(reversed(state.get("parts", {}).values()), ""),
            }
        except BaseException as error:
            if process:
                self.kill_group(process)
            code = str(error) if isinstance(error, ProtocolError) else "runtime_failed"
            # Unexpected internal exception text is never public.
            result = {
                "outcome": "failure",
                "stopped": True,
                "error": code,
                "native_id": state.get("native_id"),
            }
        finally:
            if self.home.exists():
                try:
                    harness.release(context)
                except Exception:
                    # Cleanup failure prevents a successful secret-free recovery point.
                    self.journal.metadata("cleanup_failed", "true")
        with self.lock:
            result = redactor.clean(result)
            try:
                watermark = self.journal.append(operation, "execution.stopped", result)
                result["final_watermark"] = watermark
            except ProtocolError:
                result = {
                    "outcome": "unknown",
                    "stopped": True,
                    "error": "spool_pressure",
                    "evidence_complete": False,
                }
            self.journal.metadata("generation", int(self.journal.metadata("generation")) + 1)
            self.journal.update(operation, "terminal", result=result)
            if self.active_operation == operation:
                self.active_operation, self.process = None, None
