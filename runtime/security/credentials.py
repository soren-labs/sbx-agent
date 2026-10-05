"""Ephemeral credential materialization: dirs 0700, files 0600, explicit scrub."""

from __future__ import annotations

import os
from pathlib import Path


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_secret_file(path: Path, content: str) -> Path:
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
    os.chmod(path, 0o600)
    return path


def scrub(paths: list[Path]) -> list[str]:
    """Remove credential files; returns failures (reported, never silently ignored)."""
    failures = []
    for path in paths:
        try:
            if path.exists():
                size = path.stat().st_size
                with open(path, "r+b") as fh:
                    fh.write(b"\0" * size)
                path.unlink()
        except OSError as exc:
            failures.append(f"{path.name}: {exc.strerror}")
    return failures
