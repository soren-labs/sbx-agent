"""Lease-local PTY terminals with a single user writer and bounded output buffers.

Writers are revoked while a CLI Turn or exclusive barrier is active (RFC 03
terminal policy). Process memory/PTY state is never claimed as restorable.
"""

from __future__ import annotations

import os
import pty
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from runtime.daemon.supervisor import kill_group, managed_popen

MAX_BUFFER = 512 * 1024


class Terminal:
    def __init__(
        self, cwd: Path, home: Path, *, scope: str | None = None, startup_dir: Path | None = None
    ) -> None:
        self.id = "pty_" + uuid.uuid4().hex[:16]
        self.created_at = time.time()
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
            scope=scope,
            startup_dir=startup_dir,
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
        exited = self.proc.poll() is not None
        with self.lock:
            start = max(after, self.base) - self.base
            data = bytes(self.buffer[start:])
            offset = self.base + len(self.buffer)
        # Offsets stay on UTF-8 boundaries: bytes of a character cut by buffer trimming are
        # skipped, and a character still arriving waits for the next read instead of being
        # decoded as replacement characters.
        head = 0
        if after < self.base:
            while head < len(data) and head < 3 and data[head] & 0xC0 == 0x80:
                head += 1
        held = 0 if exited else _incomplete_tail(data)
        return {
            "data": data[head : len(data) - held].decode("utf-8", "replace"),
            "offset": offset - held,
            "closed": exited,
            "truncated": after < self.base,
        }

    def describe(self) -> dict[str, Any]:
        with self.lock:
            offset = self.base + len(self.buffer)
        return {
            "terminal_id": self.id,
            "created_at": self.created_at,
            "closed": self.proc.poll() is not None,
            "offset": offset,
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


def _incomplete_tail(data: bytes) -> int:
    """Length of a trailing UTF-8 sequence that is not complete yet (0 when none)."""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte & 0xC0 == 0x80:
            continue
        need = (
            2
            if byte & 0xE0 == 0xC0
            else 3
            if byte & 0xF0 == 0xE0
            else 4
            if byte & 0xF8 == 0xF0
            else 1
        )
        return back if need > back else 0
    return 0
