"""Durable launch receipts retained by the sole scope-owning anchor.

A locked receipt covers even pre-exec children without a scope name. Receipt
identity and spawn/reaping state are authoritative only in the operation journal;
same-UID commands can unlink or rewrite receipt files despite their advisory locks.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def scope_dir(state_dir: Path, scope: str) -> Path:
    return state_dir / "launches" / hashlib.sha256(scope.encode()).hexdigest()


@contextmanager
def _journal(path: Path):
    try:
        conn = sqlite3.connect(f"file:{path.parents[1] / 'journal.sqlite'}?mode=rw", uri=True)
        try:
            conn.execute("PRAGMA synchronous=FULL")
            with conn:
                yield conn
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise OSError("startup journal unavailable") from exc


def receipt_key(path: Path, fd: int) -> str:
    stat = os.fstat(fd)
    return f"startup:{path.name}:{stat.st_dev}:{stat.st_ino}"


def prepare_scope(state_dir: Path, scope: str) -> Path:
    root = state_dir / "launches"
    root.mkdir(exist_ok=True)
    path = scope_dir(state_dir, scope)
    existed = path.exists()
    path.mkdir(exist_ok=True)
    _sync_dir(root)
    _sync_dir(state_dir)
    if not existed:
        with _journal(path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO meta VALUES (?, ?)", (f"startup:{path.name}", "prepared")
            )
    return path


def open_receipt(path: Path) -> int:
    name = uuid.uuid4().hex
    fd = os.open(path / name, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fsync(fd)
        _sync_dir(path)  # durable before any child can inherit the receipt
        with _journal(path) as conn:
            conn.execute(
                "INSERT INTO meta VALUES (?, ?)",
                (receipt_key(path, fd), json.dumps({"name": name, "state": "prepared"})),
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def unconfirmed_startup(path: Path) -> bool:
    with _journal(path) as conn:
        tracked = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (f"startup:{path.name}",)
        ).fetchone()
        records = conn.execute(
            "SELECT key, value FROM meta WHERE key GLOB ?", (f"startup:{path.name}:*",)
        ).fetchall()
        if not tracked:
            return True  # legacy receipt contents cannot prove absence
        for key, value in records:
            record = json.loads(value)
            if record["state"] == "done":
                continue  # journaled only after the anchor reaps every descendant
            try:
                stream = (path / record["name"]).open("rb")
            except FileNotFoundError:
                return True  # unlinking cannot erase the journaled launch obligation
            with stream:
                if receipt_key(path, stream.fileno()) != key:
                    return True  # replacement does not carry the original kernel lock
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                # Read after locking: the anchor may have spawned and died since
                # enumeration. An earlier prepared state must not clear an orphan.
                current = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
                if not current or json.loads(current[0])["state"] not in ("prepared", "done"):
                    return True
    return False
