"""Read/write files inside a sandbox using only SandboxBackend.exec (or a local root)."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from control.backend import Process, SandboxBackend, SandboxHandle


def drain(proc: Process) -> int:
    for _ in proc.stdout:
        pass
    return proc.wait()


def is_local_root(handle: SandboxHandle) -> bool:
    try:
        return handle.root.is_dir()
    except OSError:
        return False


def sandbox_env(handle: SandboxHandle, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        "SBX_WORK": str(handle.root),
        "CODEX_HOME": str(handle.root / ".codex"),
        "PYTHONUNBUFFERED": "1",
    }
    # Modal ``exec(..., env=)`` replaces the process env and can hide a named
    # Secret. Forward the control-plane copy when present so Codex still auth'd.
    auth_json = os.environ.get("CODEX_AUTH_JSON")
    if auth_json:
        env["CODEX_AUTH_JSON"] = auth_json
    if extra:
        env.update(extra)
    return env


def write_file(backend: SandboxBackend, handle: SandboxHandle, relative: str, content: str) -> None:
    path = handle.root / relative
    if is_local_root(handle):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    dest = str(path)
    script = (
        "import base64, pathlib;"
        f"p=pathlib.Path({dest!r});"
        "p.parent.mkdir(parents=True, exist_ok=True);"
        f"p.write_bytes(base64.b64decode({encoded!r}))"
    )
    proc = backend.exec(handle, ["python3", "-c", script], env=sandbox_env(handle))
    code = drain(proc)
    if code != 0:
        raise RuntimeError(f"failed to write {relative} in sandbox {handle.id}")


def read_text(backend: SandboxBackend, handle: SandboxHandle, relative: str) -> str | None:
    path = handle.root / relative
    if is_local_root(handle):
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    dest = str(path)
    script = (
        "import pathlib, sys;"
        f"p=pathlib.Path({dest!r});"
        "sys.exit(2) if not p.is_file() else sys.stdout.write(p.read_text())"
    )
    proc = backend.exec(handle, ["python3", "-c", script], env=sandbox_env(handle))
    chunks = list(proc.stdout)
    code = proc.wait()
    if code != 0:
        return None
    return "\n".join(chunks)


def read_json(
    backend: SandboxBackend, handle: SandboxHandle, relative: str
) -> dict[str, Any] | None:
    raw = read_text(backend, handle, relative)
    if raw is None or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def events_path(handle: SandboxHandle) -> Path:
    return handle.root / "events.jsonl"
