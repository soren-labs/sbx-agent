"""Path validation (RFC 167 §03 file rules).

Paths MUST be root-relative; traversal, absolute paths and symlink escape
are rejected. Runtime state and credential HOME are never file roots.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath


class PathRejected(Exception):
    def __init__(self, path: str, why: str) -> None:
        super().__init__(f"path rejected: {path!r}: {why}")
        self.path = path
        self.why = why


ROOTS = ("worktree", "attachments", "exports")


def resolve(root_dir: Path, root: str, rel: str) -> Path:
    """Validate and resolve ``root/rel`` under ``root_dir``."""
    if root not in ROOTS:
        raise PathRejected(root, "unknown root")
    base = Path(root_dir) / root if root != "worktree" else Path(root_dir)
    base = base.resolve()
    pure = PurePosixPath(rel)
    if pure.is_absolute():
        raise PathRejected(rel, "absolute path")
    if any(part in ("..", "") for part in pure.parts):
        raise PathRejected(rel, "traversal")
    target = base / Path(*pure.parts)
    resolved_parent = target.parent.resolve()
    if not str(resolved_parent).startswith(str(base)) and resolved_parent != base:
        raise PathRejected(rel, "symlink escape")
    if target.exists() and target.is_symlink():
        target_resolved = target.resolve()
        if not str(target_resolved).startswith(str(base)):
            raise PathRejected(rel, "symlink escape")
    return target


def ensure_within(base: Path, target: Path) -> Path:
    base_resolved = Path(base).resolve()
    target_resolved = Path(target).resolve()
    if not str(target_resolved).startswith(str(base_resolved)) and target_resolved != base_resolved:
        raise PathRejected(str(target), "escapes base")
    return target
