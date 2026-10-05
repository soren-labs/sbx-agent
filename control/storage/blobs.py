"""Filesystem-backed content-addressed blob store (RFC 167 §04).

Blobs are keyed ``<workspace>/<digest[:2]>/<digest>``; writes are sealed
immutable bytes. The ``blobs`` table records registry metadata; this store
holds the payload itself. Dedupe is by digest — a second put of identical
bytes is a no-op.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class BlobStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, storage_key: str) -> Path:
        p = (self.root / storage_key).resolve()
        if not str(p).startswith(str(self.root.resolve())) or ".." in Path(storage_key).parts:
            raise ValueError(f"storage key escapes blob root: {storage_key!r}")
        return p

    def put(self, workspace_id: str, data: bytes) -> dict:
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        hexed = digest.split(":", 1)[1]
        storage_key = f"{workspace_id}/{hexed[:2]}/{hexed}"
        path = self._path(storage_key)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return {"digest": digest, "storage_key": storage_key, "size": len(data)}

    def read(self, storage_key: str) -> bytes:
        return self._path(storage_key).read_bytes()

    def exists(self, storage_key: str) -> bool:
        return self._path(storage_key).exists()
