"""Supervised declared services (ProjectVersion declarations; no shell interpolation)."""

from __future__ import annotations

import collections
import os
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from runtime.security.paths import safe_join
from runtime.security.redaction import Redactor


class Service:
    def __init__(self, decl: dict[str, Any], root: Path, home: Path) -> None:
        self.decl = decl
        self.root = root
        self.home = home
        self.logs: collections.deque[str] = collections.deque(maxlen=500)
        self.proc: subprocess.Popen[bytes] | None = None
        self.restarts = 0
        self.wanted = False
        self.lock = threading.Lock()

    def start(self) -> None:
        with self.lock:
            self.wanted = True
            if self.proc is not None and self.proc.poll() is None:
                return
            cwd = (
                safe_join(self.root, self.decl.get("cwd") or ".")
                if self.decl.get("cwd") not in (None, ".")
                else self.root
            )
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(self.home),
                "LANG": "C.UTF-8",
            }
            if self.decl.get("port"):
                env["PORT"] = str(self.decl["port"])
            self.home.mkdir(parents=True, exist_ok=True)
            self.proc = subprocess.Popen(
                self.decl["argv"],
                cwd=str(cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        redactor = Redactor()
        for raw in proc.stdout:
            self.logs.append(redactor.text(raw.decode("utf-8", "replace").rstrip("\n"))[:2000])
        code = proc.wait()
        if (
            self.wanted
            and self.decl.get("restart", "on-failure") == "on-failure"
            and code != 0
            and self.restarts < 3
        ):
            self.restarts += 1
            time.sleep(1.0)
            self.start()

    def stop(self) -> bool:
        with self.lock:
            self.wanted = False
            if self.proc is None or self.proc.poll() is not None:
                return True
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            return True

    def status(self) -> dict[str, Any]:
        running = self.proc is not None and self.proc.poll() is None
        healthy = None
        health = self.decl.get("health") or {}
        if running and self.decl.get("port") and health.get("path"):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.decl['port']}{health['path']}", timeout=2
                ) as r:
                    healthy = 200 <= r.status < 400
            except Exception:
                healthy = False
        state = (
            "ready"
            if running and healthy is not False
            else ("starting" if running else ("stopped" if not self.wanted else "failed"))
        )
        return {
            "name": self.decl["name"],
            "state": state,
            "running": running,
            "healthy": healthy,
            "port": self.decl.get("port"),
            "restarts": self.restarts,
            "exit_code": None if running or self.proc is None else self.proc.returncode,
        }
