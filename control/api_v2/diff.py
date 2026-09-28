"""Unified-diff parsing for the Session changes surface (SOR-259).

The durable revision artifact already carries the exact delta produced by
a run as ``patch.diff`` (``git diff --binary <base>``). This module turns
that patch into the file-level view the console renders — a compact file
list with per-file +/- stats up front, and each file's diff section on
demand. Nothing here touches a sandbox: it is a pure text transform over
already-sanitized durable bytes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FileDiff:
    """One file's parsed view of a unified diff section."""

    path: str
    status: str  # "added" | "modified" | "deleted" | "renamed"
    additions: int
    deletions: int
    body: str
    old_path: str | None = None


def _unquote(path: str) -> str:
    """``git`` C-quotes oddball paths ("a/weird\\tname"); only the plain
    ``a/``/``b/``-prefixed form needs handling here."""
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def _section_paths(header: str) -> tuple[str | None, str | None]:
    """``diff --git a/old b/new`` → (old, new) with prefixes stripped."""
    rest = header[len("diff --git ") :]
    if rest.startswith("a/") or rest.startswith('"a/'):
        # Split at the last " b/" boundary so paths containing it survive.
        idx = rest.rfind(" b/")
        if idx >= 0:
            return _unquote(rest[:idx]), _unquote(rest[idx + 1 :])
        idx = rest.rfind('" b/')
        if idx >= 0:
            return _unquote(rest[: idx + 1]), _unquote(rest[idx + 2 :])
    return None, None


def parse_unified_diff(text: str) -> list[FileDiff]:
    """Parse a ``git diff`` patch into per-file records in patch order."""
    if not text:
        return []
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("diff --git ")]
    files: list[FileDiff] = []
    for pos, start in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
        section = lines[start:end]
        old_path, new_path = _section_paths(section[0])
        status = "modified"
        additions = 0
        deletions = 0
        in_hunk = False
        for line in section[1:]:
            if line.startswith("new file mode"):
                status = "added"
            elif line.startswith("deleted file mode"):
                status = "deleted"
            elif line.startswith("rename from "):
                status = "renamed"
                old_path = line[len("rename from ") :]
            elif line.startswith("rename to "):
                status = "renamed"
                new_path = line[len("rename to ") :]
            elif line.startswith("similarity index") or line.startswith("dissimilarity index"):
                status = "renamed"
            elif line.startswith("--- "):
                candidate = _unquote(line[4:].strip())
                if candidate != "/dev/null" and old_path is None:
                    old_path = candidate
            elif line.startswith("+++ "):
                candidate = _unquote(line[4:].strip())
                if candidate != "/dev/null":
                    new_path = candidate
            elif line.startswith("@@"):
                in_hunk = True
            elif in_hunk:
                if line.startswith("+"):
                    additions += 1
                elif line.startswith("-"):
                    deletions += 1
        path = new_path or old_path or ""
        if not path:
            continue
        body = "\n".join(section)
        files.append(
            FileDiff(
                path=path,
                status=status,
                additions=additions,
                deletions=deletions,
                body=body,
                old_path=old_path if status == "renamed" else None,
            )
        )
    return files
