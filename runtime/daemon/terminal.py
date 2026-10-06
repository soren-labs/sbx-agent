"""Lease-local PTY terminals with a single user writer and bounded output buffers.

Writers are revoked while a CLI Turn or exclusive barrier is active (RFC 03
terminal policy). Process memory/PTY state is never claimed as restorable.
"""

from __future__ import annotations

import os
import pty
import threading
import uuid
from pathlib import Path
from typing import Any

from runtime.daemon.supervisor import kill_group, managed_popen

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
        self.proc = managed_popen(
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
        self.closed = False
        self.buffer = bytearray()
        self.base = 0  # absolute offset of buffer[0]
        self.lock = threading.Lock()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        while not self.closed:
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

    def close(self) -> bool:
        if self.closed:
            return True
        stopped = kill_group(self.proc.pid)
        if not stopped:
            return False
        self.proc.wait(timeout=2)
        self.closed = True
        try:
            os.close(self.master)
        except OSError:
            pass
        return True
