"""Root-relative path validation against traversal, absolute and symlink escape."""

from __future__ import annotations

import os
from pathlib import Path


class PathEscape(ValueError):
    pass


def safe_join(root: Path, rel: str) -> Path:
    if not isinstance(rel, str) or rel.startswith(("/", "\\")) or "\x00" in rel:
        raise PathEscape("path must be root-relative")
    parts = Path(rel).parts
    if any(p == ".." for p in parts):
        raise PathEscape("parent traversal is not allowed")
    root_real = Path(os.path.realpath(root))
    candidate = Path(os.path.realpath(root_real / rel))
    if candidate != root_real and root_real not in candidate.parents:
        raise PathEscape("path escapes its root")
    return candidate
