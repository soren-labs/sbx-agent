"""Canonical ChangeSet manifest and subject digest (RFC 05; vectors in docs/specs/unified/manifests).

subject_digest = "sha256:" + SHA-256 over canonical JSON of the versioned manifest:
repository identity (or projectless namespace), base SHA, baseline tree, exact
resulting files (normalized path order, type, mode, content digest), optional
head/tree. Timestamps and remote URLs are excluded.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

MANIFEST_VERSION = "sbx-changeset/1"
FILE_TYPES = ("file", "symlink", "deleted")


def canonical_manifest(
    *,
    repository: str | None,
    base_sha: str,
    baseline_tree: str | None,
    files: list[dict[str, Any]],
    tree_sha: str | None,
    head_sha: str | None = None,
) -> dict[str, Any]:
    normalized = []
    seen = set()
    for entry in files:
        path = entry["path"]
        if path in seen or path.startswith("/") or ".." in path.split("/"):
            raise ValueError(f"invalid or duplicate manifest path {path!r}")
        seen.add(path)
        kind = entry["type"]
        if kind not in FILE_TYPES:
            raise ValueError(f"bad file type {kind}")
        normalized.append(
            {
                "path": path,
                "type": kind,
                "mode": None if kind == "deleted" else entry.get("mode"),
                "digest": None if kind == "deleted" else entry.get("digest"),
            }
        )
    normalized.sort(key=lambda e: e["path"].encode("utf-8"))
    return {
        "manifest_version": MANIFEST_VERSION,
        "repository": repository or "projectless",
        "base_sha": base_sha,
        "baseline_tree": baseline_tree,
        "files": normalized,
        "tree_sha": tree_sha,
        "head_sha": head_sha,
    }


def subject_digest(manifest: dict[str, Any]) -> str:
    data = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def content_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()
