"""Immutable ChangeSets and canonical subject digests (RFC 167 §05).

A ChangeSet is sealed immutable content — independently readable and
deliverable after author compute disappears. ``subject_digest`` is SHA-256 over
a versioned canonical manifest.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass, field

from .errors import DomainError

MANIFEST_VERSION = 1


class CaptureOrigin(enum.StrEnum):
    AUTOMATIC = "automatic"
    EXPLICIT = "explicit"
    SALVAGE = "salvage"


@dataclass(frozen=True)
class ChangeSetFile:
    """One canonical file entry in a sealed manifest."""

    path: str  # canonical relative path, normalized separators, no leading /
    file_type: str  # file|symlink|deleted
    mode: int  # octal permission bits
    content_digest: str | None = None  # sha256:<hex> for regular files
    symlink_target: str | None = None
    blob_id: str | None = None

    def __post_init__(self) -> None:
        p = self.path
        if not p or p.startswith("/") or ".." in p.split("/") or "\\" in p or p.endswith("/"):
            raise DomainError("validation_failed", f"non-canonical changeset path {p!r}")

    def to_entry(self) -> dict:
        d: dict = {"path": self.path, "type": self.file_type, "mode": self.mode}
        if self.content_digest is not None:
            d["content_digest"] = self.content_digest
        if self.symlink_target is not None:
            d["symlink_target"] = self.symlink_target
        return d


def canonical_manifest(
    *,
    repository: str | None,
    projectless_namespace: str | None,
    base_sha: str | None,
    baseline_digest: str | None,
    head_sha: str | None,
    tree_sha: str | None,
    files: list[ChangeSetFile],
) -> dict:
    """Versioned canonical manifest (no timestamps, no remote URLs)."""
    return {
        "manifest_version": MANIFEST_VERSION,
        "repository": repository,
        "projectless_namespace": projectless_namespace,
        "base_sha": base_sha,
        "baseline_digest": baseline_digest,
        "head_sha": head_sha,
        "tree_sha": tree_sha,
        "files": [f.to_entry() for f in sorted(files, key=lambda f: f.path)],
    }


def subject_digest(manifest: dict) -> str:
    """SHA-256 over the canonical manifest serialization."""
    blob = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


@dataclass
class ChangeSet:
    id: str
    workspace_id: str
    session_id: str
    worktree_id: str
    worktree_generation: int
    subject_digest_value: str
    source_turn_id: str | None = None
    repository: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    tree_sha: str | None = None
    capture_origin: CaptureOrigin = CaptureOrigin.EXPLICIT
    automatic_eligible: bool = False
    manifest: dict = field(default_factory=dict)
    payload_refs: dict = field(default_factory=dict)
    created_at: object = None

    def require_automatic_eligible(self) -> None:
        if not self.automatic_eligible:
            raise DomainError(
                "invalid_state",
                "ChangeSet is not eligible for automatic delivery",
                details={"changeset_id": self.id, "origin": self.capture_origin.value},
            )


def automatic_eligible(origin: CaptureOrigin, turn_succeeded: bool) -> bool:
    """Auto-shipping requires a durably succeeded Turn; salvage never qualifies."""
    if origin is CaptureOrigin.SALVAGE:
        return False
    return turn_succeeded
