"""Credential-free identity shared by hosted reconciliation and publication."""

import hashlib
import os
from pathlib import Path


def runtime_build_identity(version: str, *, root: Path | None = None) -> str:
    root = root or Path(__file__).resolve().parent
    digest = hashlib.sha256(version.encode())
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".sh", ".txt"}:
            relative = str(path.relative_to(root))
            digest.update(relative.encode() + b"\0" + path.read_bytes() + b"\0")
    # These are the supported image build overrides, never credentials.
    for key in ("SBX_CODEX_VERSION", "SBX_NPM_REGISTRY"):
        digest.update(key.encode() + b"\0" + os.environ.get(key, "").encode() + b"\0")
    return digest.hexdigest()
