"""Linux subreaper anchor for a managed command (launched by supervisor.py).

The command's exit status is sent separately from the anchor's lifetime. Orphans
remain children of this anchor even after setsid, env replacement or prctl changes,
until the supervisor confirms their termination. No credential inspection is needed.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys


def scope_name(scope: str) -> str:
    # Linux comm is limited to 15 bytes; this is a public attribution identifier.
    return "sbx-" + hashlib.sha256(scope.encode()).hexdigest()[:11]


def main() -> None:
    scope, status_fd, receipt_fd, journal_path, receipt_key, *argv = sys.argv[1:]
    fd = int(status_fd)
    receipt = int(receipt_fd)

    def mark(data: bytes) -> None:
        if receipt >= 0:
            os.lseek(receipt, 0, os.SEEK_SET)
            os.write(receipt, data)
            os.ftruncate(receipt, len(data))
            os.fsync(receipt)
            # Commit spawn intent before Popen, and completion only after reaping.
            # Receipt files are same-UID mutable and are never termination proof.
            conn = sqlite3.connect(f"file:{journal_path}?mode=rw", uri=True)
            try:
                conn.execute("PRAGMA synchronous=FULL")
                with conn:
                    conn.execute(
                        "UPDATE meta SET value = json_set(value, '$.state', ?) WHERE key = ?",
                        (data.decode(), receipt_key),
                    )
            finally:
                conn.close()

    def report(data: bytes) -> None:
        try:
            os.write(fd, data)
        except BrokenPipeError:
            pass  # daemon death must not drop ownership of surviving descendants

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "cannot establish process ownership")
        if libc.prctl(15, scope_name(scope).encode(), 0, 0, 0) != 0:  # PR_SET_NAME
            raise OSError(ctypes.get_errno(), "cannot name process scope")
        # Keep ownership until descendants exit. Exec resets this callable handler
        # in the command, so the command still receives ordinary SIGTERM behavior.
        signal.signal(signal.SIGTERM, lambda *_: None)
        mark(b"spawning")  # durable before any command can outlive this anchor
        proc = subprocess.Popen(argv, close_fds=True, start_new_session=True)
    except OSError as exc:
        mark(b"done")
        report((json.dumps({"errno": exc.errno}) + "\n").encode())
        return
    report(b"{}\n")
    code = proc.wait()
    report(f"{code}\n".encode())
    os.close(fd)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.close()
    # CPython's standard streams do not own their underlying file descriptors.
    # Release the anchor's copies so they cannot delay EOF or service restart.
    for stream_fd in (0, 1, 2):
        os.close(stream_fd)
    while True:
        try:
            os.waitpid(-1, 0)
        except ChildProcessError:
            break
    mark(b"done")


if __name__ == "__main__":
    main()
