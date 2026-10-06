"""Durable launch receipts retained by the sole scope-owning anchor.

A locked receipt covers even pre-exec children without a scope name. An unlocked
empty receipt proves command spawn never began; ``done`` proves full reaping.
``spawning`` without an owner remains ambiguous (the anchor may have died with
orphan writers), so absence of named processes alone must not clear it.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import uuid
from pathlib import Path


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def scope_dir(state_dir: Path, scope: str) -> Path:
    return state_dir / "launches" / hashlib.sha256(scope.encode()).hexdigest()


def prepare_scope(state_dir: Path, scope: str) -> Path:
    root = state_dir / "launches"
    root.mkdir(exist_ok=True)
    path = scope_dir(state_dir, scope)
    path.mkdir(exist_ok=True)
    _sync_dir(root)
    _sync_dir(state_dir)
    return path


def open_receipt(path: Path) -> int:
    fd = os.open(path / uuid.uuid4().hex, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fsync(fd)
        _sync_dir(path)  # durable before any child can inherit the receipt
    except BaseException:
        os.close(fd)
        raise
    return fd


def unconfirmed_startup(path: Path) -> bool:
    for receipt in path.iterdir():
        with receipt.open("rb") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            if stream.read() not in (b"", b"done"):
                return True
    return False
