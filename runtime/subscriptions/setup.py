"""Setup VM supervisor: one official subscription login against a mounted profile.

Runs as the VM's main process. It drives the provider's own CLI (device login, login
status, one real verification call), flushes the profile Volume and publishes a small
state file for the control plane. Only CLI terminal output is inspected: credential
files are never opened, listed, copied or printed, and the state file carries only the
verification URL, the one-time user code (until it is used), coarse outcome codes and the
model catalog the authenticated CLI reports.

The spec is provider data supplied by the control plane's adapter, so this module holds
no provider knowledge. Every CLI call closes stdin: an open stdin stalls official CLIs.
The one exception is a provider whose official remote sign-in is a terminal screen that
shows a URL and asks for the authorization code the browser displays: that screen runs
under a PTY, and the code the user submits is typed into it once and kept nowhere.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
# Terminal control output of a full-screen CLI: OSC strings (hyperlinks, titles) and CSI.
TERMINAL = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?<>=]*[a-zA-Z]")
# What a pasted authorization code may contain; anything else is never typed into the CLI.
INPUT_PATTERN = r"[A-Za-z0-9._~/+=-]{8,512}"
TERMINAL_PHASES = ("succeeded", "failed", "expired")
# Coarse, non-identifying causes surfaced to the user when verification fails.
DIAGNOSTICS = {
    "usage_limited": ("rate limit", "usage limit", "429"),
    "unauthorized": ("unauthorized", "401", "authentication failed", "not logged in"),
    "network": ("error sending request", "failed to connect", "stream disconnected"),
    "tls": ("certificate", "unknownissuer"),
}


def _now() -> datetime:
    return datetime.now(UTC)


def _dig(value: Any, path: list[str]) -> Any:
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _reaped(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return True
        except ChildProcessError:
            return True
        time.sleep(0.1)
    return False


class Supervisor:
    def __init__(self, spec: dict[str, Any], *, sleep: Any = time.sleep) -> None:
        self.spec = spec
        self.sleep = sleep
        self.work = Path(spec.get("work_dir", "/tmp/sbx-setup"))
        self.state_path = Path(spec.get("state_path", str(self.work / "state.json")))
        self.log_path = self.work / "login-output"
        # Written by the control plane when the user submits the code the browser showed.
        self.input_path = Path(spec.get("input_path", str(self.work / "login-input")))
        self.state: dict[str, Any] = {"phase": "starting", "mode": spec.get("mode", "login")}
        self.started = time.monotonic()

    # ------------------------------------------------------------------ state file
    def publish(self, **changes: Any) -> None:
        self.state.update(changes, updated_at=_now().isoformat())
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        os.replace(tmp, self.state_path)

    def finish(self, phase: str, error: str | None = None) -> str:
        # The one-time code is useless after the attempt ends: never keep it around.
        self.state.pop("user_code", None)
        self.state.pop("input", None)
        self.log_path.unlink(missing_ok=True)
        self.input_path.unlink(missing_ok=True)
        self.publish(phase=phase, error=error)
        return phase

    # ------------------------------------------------------------------ CLI calls
    def call(
        self, argv: list[str], timeout: float, cwd: str | None = None
    ) -> tuple[int | None, str]:
        try:
            done = subprocess.run(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None, ""
        return done.returncode, done.stdout + done.stderr

    def catalog(self) -> dict[str, Any] | None:
        """Model catalog from the authenticated CLI's own interface.

        Either JSON-RPC lines on stdio (only the adapter-listed fields of each item are
        returned) or, for a CLI with no machine interface, the lines its models command
        prints. ``None`` when the CLI offers no trustworthy answer, so nothing is guessed.
        """
        spec = self.spec.get("catalog")
        if not spec:
            return None
        if spec.get("kind") == "text":
            return self._catalog_text(spec)
        try:
            process = subprocess.Popen(
                spec["argv"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return None
        try:
            payload = "".join(json.dumps(request) + "\n" for request in spec["requests"])
            process.stdin.write(payload.encode())
            process.stdin.flush()
            deadline = time.monotonic() + float(spec.get("timeout", 60))
            pending = b""
            # Unbuffered reads: a buffered reader would hide lines from ``select``.
            while time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [], 1.0)
                if not ready:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    return None
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    found = self._catalog_result(line, spec)
                    if found is not None:
                        return found or None
            return None
        except (OSError, ValueError):
            return None
        finally:
            process.kill()
            process.wait()

    @staticmethod
    def _catalog_result(line: bytes, spec: dict[str, Any]) -> dict[str, Any] | None:
        """``None`` for unrelated lines, ``{}`` for an unusable answer, else the catalog."""
        try:
            message = json.loads(line)
        except ValueError:
            return None
        if not isinstance(message, dict) or message.get("id") != spec["result_id"]:
            return None
        result = message.get("result")
        items = _dig(result, spec.get("items_path") or [spec["items_key"]])
        if not isinstance(items, list):
            return {}
        fields = spec["item_fields"]
        found: dict[str, Any] = {
            "items": [
                {k: item[k] for k in fields if k in item}
                for item in items[:200]
                if isinstance(item, dict)
            ],
            "complete": not result.get(spec.get("cursor_key", "nextCursor")),
        }
        # Single values next to the list, e.g. the model the CLI currently defaults to.
        for name, path in (spec.get("value_paths") or {}).items():
            value = _dig(result, path)
            if isinstance(value, str | int | float | bool):
                found[name] = value
        return found

    def _catalog_text(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        """Printed lines of a models command, for a CLI with no machine interface."""
        code, output = self.call(spec["argv"], float(spec.get("timeout", 60)))
        if code != 0:
            return None
        lines = [line.strip()[:300] for line in ANSI.sub("", output).splitlines()]
        found: dict[str, Any] = {"lines": [line for line in lines if line][:200], "complete": True}
        return found if found["lines"] else None

    def logged_in(self) -> bool:
        code, output = self.call(
            self.spec["status_argv"], float(self.spec.get("status_timeout", 30))
        )
        lowered = output.lower()
        # Some CLIs exit 0 while saying they are signed out: their own words decide.
        if any(marker.lower() in lowered for marker in self.spec.get("status_fail", [])):
            return False
        return code == 0 and self.spec["status_ok"] in output

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    # ------------------------------------------------------------------ phases
    def login(self) -> str | None:
        """Official device login. Returns a terminal phase, or ``None`` once logged in."""
        spec = self.spec
        with open(self.log_path, "w") as log:
            process = subprocess.Popen(
                spec["login_argv"], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT
            )
        expires_at: datetime | None = None
        next_status = 0.0
        exited_at: float | None = None
        try:
            while True:
                output = ANSI.sub("", self.log_path.read_text(errors="replace"))
                if "user_code" not in self.state:
                    code = re.search(spec["code_pattern"], output)
                    url = re.search(spec["url_pattern"], output)
                    if code and url:
                        minutes = re.search(spec["expiry_pattern"], output, re.I)
                        seconds = (
                            int(minutes.group(1)) * 60
                            if minutes
                            else int(spec["default_code_seconds"])
                        )
                        expires_at = _now() + timedelta(seconds=seconds)
                        self.publish(
                            phase="awaiting_user",
                            verification_url=url.group(0),
                            user_code=code.group(0),
                            code_expires_at=expires_at.isoformat(),
                        )
                exited = process.poll() is not None
                if exited and exited_at is None:
                    exited_at = time.monotonic()
                # The CLI's own status is the authority: a login process that exited (or a
                # stale one) never overrides a login that the provider has accepted.
                if exited or time.monotonic() >= next_status:
                    next_status = time.monotonic() + float(spec.get("status_interval", 3))
                    if self.logged_in():
                        return None
                if exited_at is not None and time.monotonic() - exited_at > float(
                    spec.get("exit_grace", 6)
                ):
                    denied = any(w in output.lower() for w in ("denied", "declined", "rejected"))
                    return self.finish("failed", "login_denied" if denied else "login_failed")
                if (expires_at and _now() >= expires_at) or self.elapsed() > float(
                    spec["deadline_seconds"]
                ):
                    return self.finish("expired", "code_expired")
                self.sleep(0.5)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

    def take_input(self) -> str | None:
        """The code the user submitted, read once and removed; malformed input is dropped."""
        try:
            text = self.input_path.read_text(errors="replace").strip()
        except OSError:
            return None
        self.input_path.unlink(missing_ok=True)
        pattern = self.spec.get("input_pattern", INPUT_PATTERN)
        return text if re.fullmatch(pattern, text) else None

    def login_pty(self) -> str | None:
        """Official sign-in screen under a PTY: relay its URL, type the user's code back.

        Terminal output stays in memory (it echoes the code), so nothing of it reaches
        disk or the state file. Returns a terminal phase, or ``None`` once logged in.
        """
        spec = self.spec
        ui = spec["pty"]
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.execvp(spec["login_argv"][0], spec["login_argv"])
            finally:
                os._exit(127)
        # Wide enough that the CLI prints the sign-in URL on one line.
        size = struct.pack("HHHH", int(ui.get("rows", 50)), int(ui.get("cols", 4000)), 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
        steps = list(ui.get("steps", []))
        output = ""
        submitted_at: float | None = None
        exited_at: float | None = None
        next_status = 0.0
        try:
            while True:
                ready, _, _ = select.select([fd], [], [], 0.5)
                if ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        chunk = b""
                    # Keep a bounded tail; a full-screen CLI redraws continuously.
                    output = (output + chunk.decode(errors="replace"))[-400_000:]
                text = TERMINAL.sub("", output)
                # Scripted key presses that reach the sign-in screen (e.g. choose a method).
                while steps and steps[0]["expect"] in text:
                    os.write(fd, steps.pop(0)["send"].encode())
                if "verification_url" not in self.state:
                    url = re.search(spec["url_pattern"], text)
                    if url and ui["prompt"] in text:
                        expires_at = _now() + timedelta(seconds=int(spec["default_code_seconds"]))
                        self.publish(
                            phase="awaiting_user",
                            verification_url=url.group(0),
                            input=ui["input"],
                            code_expires_at=expires_at.isoformat(),
                        )
                elif submitted_at is None:
                    code = self.take_input()
                    if code:
                        os.write(fd, code.encode() + b"\r")
                        submitted_at = time.monotonic()
                        self.state.pop("input", None)
                        self.publish(phase="verifying")
                if exited_at is None:
                    try:
                        if os.waitpid(pid, os.WNOHANG)[0] == pid:
                            exited_at = time.monotonic()
                            pid = 0
                    except ChildProcessError:
                        exited_at, pid = time.monotonic(), 0
                # The CLI's own status is the authority, and only once a code was entered.
                waiting = submitted_at is not None or exited_at is not None
                if waiting and time.monotonic() >= next_status:
                    next_status = time.monotonic() + float(spec.get("status_interval", 3))
                    if self.logged_in():
                        return None
                if exited_at is not None and time.monotonic() - exited_at > float(
                    spec.get("exit_grace", 6)
                ):
                    return self.finish("failed", "login_failed")
                if submitted_at is not None and time.monotonic() - submitted_at > float(
                    ui.get("accept_seconds", 90)
                ):
                    # The CLI did not accept the code (wrong, reused or expired).
                    return self.finish("failed", "code_rejected")
                if self.elapsed() > float(spec["deadline_seconds"]):
                    return self.finish("expired", "code_expired")
        finally:
            output = ""
            if pid:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.kill(pid, sig)
                    except OSError:
                        break
                    if _reaped(pid, 5):
                        break
            os.close(fd)

    def verify_and_sync(self) -> str:
        spec = self.spec
        self.state.pop("user_code", None)
        self.publish(phase="verifying")
        Path(spec["verify_cwd"]).mkdir(parents=True, exist_ok=True)
        # Not every CLI has a working-directory flag: the call itself runs in the empty one.
        code, output = self.call(
            spec["verify_argv"], float(spec.get("verify_timeout", 180)), spec["verify_cwd"]
        )
        lowered = output.lower()
        causes = [name for name, words in DIAGNOSTICS.items() if any(w in lowered for w in words)]
        verified = code == 0 and spec["verify_ok"] in output
        if not verified and not (causes == ["usage_limited"] and self.logged_in()):
            # A plan at its usage limit is still an authorized login; anything else is not.
            return self.finish("failed", "verification_failed:" + (",".join(causes) or "unknown"))
        version_code, version = self.call(spec["version_argv"], 20)
        # Volume v2 commits on an explicit sync of the mount (not on VM termination).
        synced = self.call(["sync", spec["profile_dir"]], 60)[0] == 0
        if not synced:
            return self.finish("failed", "profile_sync_failed")
        self.publish(
            cli_version=version.strip()[:80] if version_code == 0 else None,
            catalog=self.catalog(),
            real_model_call=verified,
            warning=None if verified else "usage_limited",
            profile_synced=True,
        )
        return self.finish("succeeded")

    def run(self) -> str:
        os.umask(0o077)
        self.work.mkdir(parents=True, exist_ok=True)
        for directory in self.spec.get("ensure_dirs", []):
            Path(directory).mkdir(parents=True, exist_ok=True)
        self.publish()
        if self.spec.get("mode") == "verify":
            phase = (
                self.verify_and_sync()
                if self.logged_in()
                else self.finish("failed", "not_logged_in")
            )
        else:
            login = self.login_pty if self.spec.get("pty") else self.login
            phase = login() or self.verify_and_sync()
        # Stay observable until the control plane has read the outcome and stops the VM.
        deadline = time.monotonic() + float(self.spec.get("linger_seconds", 180))
        while time.monotonic() < deadline:
            self.sleep(1.0)
        return phase


def main(argv: list[str]) -> int:
    spec = json.loads(os.environ["SBX_SETUP_SPEC"] if len(argv) < 2 else argv[1])
    return 0 if Supervisor(spec).run() == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
