"""Paths, session.json, and events.jsonl I/O under ``$SBX_WORK``."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from runtime.runner.constants import DEFAULT_WORK

# SOR-174: the control plane declares the WorkspaceRecord workdir for a turn
# through this env var (sandbox-root-relative, e.g. ``repo``). Provider CLIs
# run inside it; ``$SBX_WORK`` itself stays the runner state root.
WORKDIR_ENV = "SBX_WORKDIR"


def work_root() -> Path:
    raw = os.environ.get("SBX_WORK") or DEFAULT_WORK
    return Path(raw)


def agent_workdir(root: Path | None = None) -> Path:
    """Provider-CLI working directory: ``root/<SBX_WORKDIR>`` when the
    control plane declared a workspace workdir, ``root`` itself otherwise.

    ``SBX_WORKDIR`` is written from a ``WorkspaceRecord.workdir`` relpath
    already validated by the control plane; anything absolute or escaping
    the root falls back to the state root rather than running elsewhere.
    """
    base = root if root is not None else work_root()
    raw = os.environ.get(WORKDIR_ENV) or ""
    if raw:
        rel = PurePosixPath(raw)
        if rel.parts and not rel.is_absolute() and ".." not in rel.parts:
            return base / str(rel)
    return base


def codex_home(root: Path | None = None) -> Path:
    base = root if root is not None else work_root()
    raw = os.environ.get("CODEX_HOME")
    if raw:
        return Path(raw)
    return base / ".codex"


def sandbox_home(root: Path | None = None) -> Path:
    """``$HOME`` inside the sandbox: ``$SBX_WORK/home`` (filesystem.md v2).

    In production ``HOME`` is already ``$SBX_WORK/home``; honour it when it
    resolves inside ``root``. In tests ``HOME`` is isolated elsewhere, so the
    credential blob restores under ``root/home`` either way.
    """
    base = root if root is not None else work_root()
    raw = os.environ.get("HOME")
    if raw:
        home = Path(raw).resolve()
        try:
            if home.is_relative_to(base.resolve()):
                return Path(raw)
        except OSError:
            pass
    return base / "home"


def ensure_layout(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "inbox").mkdir(exist_ok=True)
    (root / "turns").mkdir(exist_ok=True)
    codex_home(root).mkdir(parents=True, exist_ok=True)
    sandbox_home(root).mkdir(parents=True, exist_ok=True)


def events_path(root: Path) -> Path:
    return root / "events.jsonl"


def events_raw_path(root: Path) -> Path:
    return root / "events.raw.jsonl"


def session_path(root: Path) -> Path:
    return root / "session.json"


def default_session() -> dict[str, Any]:
    return {
        "codex_session_id": None,
        "native_session_id": None,
        "provider": "codex",
        "account_id": None,
        "credential_files": [],
        "turn": 0,
        "pid": None,
        "model": None,
        "auth": None,
    }


def load_session(root: Path) -> dict[str, Any]:
    path = session_path(root)
    if not path.is_file():
        return default_session()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_session()
    if not isinstance(data, dict):
        return default_session()
    merged = default_session()
    merged.update(data)
    return merged


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save_session(root: Path, session: dict[str, Any]) -> None:
    atomic_write(session_path(root), json.dumps(session, ensure_ascii=False) + "\n")


def emit_native(root: Path, line: str) -> None:
    """Append one native CLI stdout line to ``events.raw.jsonl``."""
    text = line if line.endswith("\n") else line + "\n"
    with events_raw_path(root).open("a", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()


def emit_raw(root: Path, line: str) -> None:
    """Append one canonical event line to ``events.jsonl`` and runner stdout."""
    text = line if line.endswith("\n") else line + "\n"
    with events_path(root).open("a", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
    print(text, end="", flush=True)


def emit(root: Path, obj: dict[str, Any]) -> None:
    emit_raw(root, json.dumps(obj, ensure_ascii=False))
