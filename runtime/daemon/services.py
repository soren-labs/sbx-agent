"""Supervised declared services (ProjectVersion declarations; no shell interpolation)."""

from __future__ import annotations

import collections
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from runtime.daemon.supervisor import ManagedProcess, kill_group, managed_popen
from runtime.security.paths import safe_join
from runtime.security.redaction import Redactor


class Service:
    def __init__(
        self,
        decl: dict[str, Any],
        root: Path,
        home: Path,
        *,
        known: set[str] | None = None,
        scope: str | None = None,
        startup_dir: Path | None = None,
    ) -> None:
        self.decl = decl
        self.scope = scope
        self.startup_dir = startup_dir
        self.known = known if known is not None else set()
        self.root = root
        self.home = home
        self.logs: collections.deque[str] = collections.deque(maxlen=500)
        self.proc: ManagedProcess | None = None
        self.generations: list[ManagedProcess] = []
        self.restarts = 0
        self.wanted = False
        self.lock = threading.Lock()

    def start(self, *, restart: bool = False) -> None:
        with self.lock:
            if restart and not self.wanted:
                return
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
            self.proc = managed_popen(
                self.decl["argv"],
                cwd=str(cwd),
                env=env,
                scope=self.scope,
                startup_dir=self.startup_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.generations.append(self.proc)
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc: ManagedProcess) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            self.logs.append(
                Redactor(self.known).text(raw.decode("utf-8", "replace").rstrip("\n"))[:2000]
            )
        code = proc.wait()
        if (
            self.wanted
            and self.decl.get("restart", "on-failure") == "on-failure"
            and code != 0
            and self.restarts < 3
        ):
            self.restarts += 1
            time.sleep(1.0)
            self.start(restart=True)

    def stop(self) -> bool:
        with self.lock:
            self.wanted = False
            pending = []
            for proc in self.generations:
                if kill_group(proc.pid):
                    proc.wait(timeout=2)
                else:
                    pending.append(proc)
            self.generations = pending
            return not pending

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
