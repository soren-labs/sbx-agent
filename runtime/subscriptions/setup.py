"""Setup VM supervisor: one official subscription login against a mounted profile.

Runs as the VM's main process. It drives the provider's own CLI (device login, login
status, one real verification call), flushes the profile Volume and publishes a small
state file for the control plane. Only CLI terminal output is inspected: credential
files are never opened, listed, copied or printed, and the state file carries only the
verification URL, the one-time user code (until it is used) and coarse outcome codes.

The spec is provider data supplied by the control plane's adapter, so this module holds
no provider knowledge. Every CLI call closes stdin: an open stdin stalls official CLIs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
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


class Supervisor:
    def __init__(self, spec: dict[str, Any], *, sleep: Any = time.sleep) -> None:
        self.spec = spec
        self.sleep = sleep
        self.work = Path(spec.get("work_dir", "/tmp/sbx-setup"))
        self.state_path = Path(spec.get("state_path", str(self.work / "state.json")))
        self.log_path = self.work / "login-output"
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
        self.log_path.unlink(missing_ok=True)
        self.publish(phase=phase, error=error)
        return phase

    # ------------------------------------------------------------------ CLI calls
    def call(self, argv: list[str], timeout: float) -> tuple[int | None, str]:
        try:
            done = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None, ""
        return done.returncode, done.stdout + done.stderr

    def logged_in(self) -> bool:
        code, output = self.call(self.spec["status_argv"], 30)
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

    def verify_and_sync(self) -> str:
        spec = self.spec
        self.state.pop("user_code", None)
        self.publish(phase="verifying")
        Path(spec["verify_cwd"]).mkdir(parents=True, exist_ok=True)
        code, output = self.call(spec["verify_argv"], float(spec.get("verify_timeout", 180)))
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
            phase = self.login() or self.verify_and_sync()
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
