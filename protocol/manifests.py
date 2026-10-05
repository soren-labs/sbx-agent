"""Portable filesystem and native-state manifest data (RFC 167 §03/§05).

Manifests describe immutable captured content; ``subject_digest`` semantics
live in the domain layer — these are the wire-safe records only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


def content_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def manifest_digest(manifest: dict) -> str:
    """Canonical digest over a manifest dict (sorted keys, compact)."""
    blob = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class FileEntry:
    path: str  # root-relative, forward slashes
    kind: str = "file"  # file|dir|symlink|deleted
    size: int | None = None
    digest: str | None = None  # sha256:... of file content
    mode: int | None = None
    link_target: str | None = None

    def to_dict(self) -> dict:
        out = {"path": self.path, "kind": self.kind}
        for key in ("size", "digest", "mode", "link_target"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    @staticmethod
    def from_dict(raw: dict) -> FileEntry:
        return FileEntry(
            path=str(raw["path"]),
            kind=str(raw.get("kind", "file")),
            size=raw.get("size"),
            digest=raw.get("digest"),
            mode=raw.get("mode"),
            link_target=raw.get("link_target"),
        )


@dataclass(frozen=True)
class FilesystemManifest:
    """Ordered content listing of a captured tree."""

    entries: tuple[FileEntry, ...] = ()
    root: str = "worktree"
    generation: int | None = None
    base_ref: str | None = None  # e.g. repository/base_sha captured at capture time

    def to_dict(self) -> dict:
        entries = sorted(self.entries, key=lambda e: e.path)
        out: dict = {
            "root": self.root,
            "entries": [e.to_dict() for e in entries],
        }
        if self.generation is not None:
            out["generation"] = self.generation
        if self.base_ref is not None:
            out["base_ref"] = self.base_ref
        return out

    def digest(self) -> str:
        return manifest_digest(self.to_dict())


@dataclass(frozen=True)
class NativeStateManifest:
    """Allowlisted native CLI state export description."""

    provider_id: str
    native_id: str
    lineage_id: str
    state_version: int = 1
    files: tuple[FileEntry, ...] = ()
    account_affinity: str | None = None
    cli_version: str | None = None

    def to_dict(self) -> dict:
        out: dict = {
            "provider_id": self.provider_id,
            "native_id": self.native_id,
            "lineage_id": self.lineage_id,
            "state_version": self.state_version,
            "files": [e.to_dict() for e in sorted(self.files, key=lambda e: e.path)],
        }
        for key in ("account_affinity", "cli_version"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    def digest(self) -> str:
        return manifest_digest(self.to_dict())


@dataclass(frozen=True)
class NativeContextBinding:
    """Verified native CLI context record (wire form — RFC 167 §03).

    A resumed CLI reporting a different ``native_id`` MUST produce
    ``context_mismatch``; automatic continuation never forges a new
    conversation.
    """

    provider_id: str
    native_id: str
    lineage_id: str
    cli_version: str | None = None
    adapter_version: str | None = None
    state_manifest_digest: str | None = None
    account_affinity: str | None = None
    checkpoint_ref: str | None = None

    def to_dict(self) -> dict:
        out = {
            "provider_id": self.provider_id,
            "native_id": self.native_id,
            "lineage_id": self.lineage_id,
        }
        for key in (
            "cli_version",
            "adapter_version",
            "state_manifest_digest",
            "account_affinity",
            "checkpoint_ref",
        ):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    @staticmethod
    def from_dict(raw: dict) -> NativeContextBinding:
        return NativeContextBinding(
            provider_id=str(raw["provider_id"]),
            native_id=str(raw["native_id"]),
            lineage_id=str(raw["lineage_id"]),
            cli_version=raw.get("cli_version"),
            adapter_version=raw.get("adapter_version"),
            state_manifest_digest=raw.get("state_manifest_digest"),
            account_affinity=raw.get("account_affinity"),
            checkpoint_ref=raw.get("checkpoint_ref"),
        )


def digest_json(value) -> str:
    """Stable digest for arbitrary JSON-compatible values."""
    return manifest_digest(value) if isinstance(value, dict) else manifest_digest({"v": value})
