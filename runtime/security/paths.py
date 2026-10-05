from pathlib import Path

from protocol.runtime import ProtocolError


def confined(root: Path, path: str) -> Path:
    rel = Path(path)
    if rel.is_absolute() or ".." in rel.parts or "\x00" in path:
        raise ProtocolError("forbidden")
    candidate = root / rel
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ProtocolError("forbidden")
    current = candidate
    while current != root:
        if current.is_symlink():
            raise ProtocolError("forbidden")
        current = current.parent
    return candidate
