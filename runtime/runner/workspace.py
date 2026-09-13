"""Paths, session.json, and events.jsonl I/O under ``$SBX_WORK``."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from runtime.runner.constants import DEFAULT_WORK


def work_root() -> Path:
    raw = os.environ.get("SBX_WORK") or DEFAULT_WORK
    return Path(raw)


def codex_home(root: Path | None = None) -> Path:
    base = root if root is not None else work_root()
    raw = os.environ.get("CODEX_HOME")
    if raw:
        return Path(raw)
    return base / ".codex"


def ensure_layout(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "inbox").mkdir(exist_ok=True)
    (root / "turns").mkdir(exist_ok=True)
    codex_home(root).mkdir(parents=True, exist_ok=True)


def events_path(root: Path) -> Path:
    return root / "events.jsonl"


def session_path(root: Path) -> Path:
    return root / "session.json"


def default_session() -> dict[str, Any]:
    return {
        "codex_session_id": None,
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


def emit_raw(root: Path, line: str) -> None:
    text = line if line.endswith("\n") else line + "\n"
    with events_path(root).open("a", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
    print(text, end="", flush=True)


def emit(root: Path, obj: dict[str, Any]) -> None:
    emit_raw(root, json.dumps(obj, ensure_ascii=False))
