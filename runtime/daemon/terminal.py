"""Lease-local PTY terminals with a single user writer and bounded output buffers.

Writers are revoked while a CLI Turn or exclusive barrier is active (RFC 03
terminal policy). Process memory/PTY state is never claimed as restorable.
"""

from __future__ import annotations

import os
import pty
import signal
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

MAX_BUFFER = 512 * 1024


class Terminal:
    def __init__(self, cwd: Path, home: Path) -> None:
        self.id = "pty_" + uuid.uuid4().hex[:16]
        master, slave = pty.openpty()
        home.mkdir(parents=True, exist_ok=True)
        env = {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "PS1": "$ ",
        }
        self.proc = subprocess.Popen(
            ["/bin/bash", "--noprofile", "--norc", "-i"],
            cwd=str(cwd),
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)
        self.master = master
        self.buffer = bytearray()
        self.base = 0  # absolute offset of buffer[0]
        self.lock = threading.Lock()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        while True:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                break
            if not data:
                break
            with self.lock:
                self.buffer += data
                if len(self.buffer) > MAX_BUFFER:
                    drop = len(self.buffer) - MAX_BUFFER
                    del self.buffer[:drop]
                    self.base += drop

    def read(self, after: int) -> dict[str, Any]:
        with self.lock:
            start = max(after, self.base) - self.base
            data = bytes(self.buffer[start:])
            offset = self.base + len(self.buffer)
        return {
            "data": data.decode("utf-8", "replace"),
            "offset": offset,
            "closed": self.proc.poll() is not None,
            "truncated": after < self.base,
        }

    def write(self, data: str) -> None:
        os.write(self.master, data.encode()[:65536])

    def close(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.close(self.master)
        except OSError:
            pass
