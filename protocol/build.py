"""Content fingerprint of the deployed daemon, wire types and CLI adapters."""

import hashlib
from pathlib import Path


def runtime_source_digest(root=None):
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    content = hashlib.sha256()
    for directory in ("protocol", "runtime/daemon", "runtime/harnesses", "runtime/security"):
        for file in sorted((root / directory).rglob("*.py")):
            content.update(file.relative_to(root).as_posix().encode() + b"\0")
            content.update(file.read_bytes())
    return "sha256:" + content.hexdigest()
