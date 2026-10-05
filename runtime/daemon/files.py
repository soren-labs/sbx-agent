import hashlib
from pathlib import Path

from protocol.runtime import ProtocolError

from runtime.security.paths import confined

LIMIT = 4_000_000
EXCLUDED = {".git", "node_modules", ".venv", ".env", "auth.json", ".modal.toml", ".codex"}


def list_files(root):
    result = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(p in EXCLUDED for p in rel.parts) or path.is_symlink() or not path.is_file():
            continue
        result.append({"path": rel.as_posix(), "size": path.stat().st_size})
        if len(result) >= 2000:
            break
    return result


def read(root: Path, name: str):
    if any(part in EXCLUDED for part in Path(name).parts):
        raise ProtocolError("forbidden")
    path = confined(root, name)
    if not path.is_file() or path.stat().st_size > LIMIT:
        raise ProtocolError("not_found")
    content = path.read_bytes()
    return {
        "path": name,
        "content": content.decode("utf-8", "replace"),
        "digest": hashlib.sha256(content).hexdigest(),
    }


def write(root, name, content, expected_digest):
    path = confined(root, name)
    if any(part in EXCLUDED for part in Path(name).parts):
        raise ProtocolError("forbidden")
    if len(content.encode()) > LIMIT:
        raise ProtocolError("quota_exhausted")
    actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    if expected_digest != actual:
        raise ProtocolError("version_conflict")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".sbx-write")
    temporary.write_text(content)
    temporary.replace(path)
    return read(root, name)
