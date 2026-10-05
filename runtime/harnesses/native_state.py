"""Native-state export helpers shared by Harness adapters (RFC 167 §03).

Native state is an allowlisted, digest-pinned file manifest — provider
sessions live under the provider's own data dir, credentials excluded
(auth files are exported through ``export_refreshed_credentials`` only when
the provider supports it; static keys never write back).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from protocol.manifests import FileEntry, NativeStateManifest

DENY_PATTERNS = ("auth.json", "credentials", ".token", "keychain")


def _denied(rel: str) -> bool:
    lowered = rel.lower()
    return any(pattern in lowered for pattern in DENY_PATTERNS)


def export_allowlisted(
    *,
    root: Path,
    prefixes: tuple[str, ...],
    provider_id: str,
    native_id: str,
    lineage_id: str,
    cli_version: str | None = None,
    max_bytes: int = 8 * 1024 * 1024,
) -> NativeStateManifest | None:
    """Hash allowlisted files under ``root`` into a NativeStateManifest.

    Returns ``None`` when no matching files exist. Files over ``max_bytes``
    are listed with ``digest=None`` and ``oversized=True`` via ``mode`` so
    validation can refuse silently-lossy exports.
    """
    entries: list[FileEntry] = []
    if not root.is_dir():
        return None
    for prefix in prefixes:
        base = root / prefix
        if not base.exists():
            continue
        candidates = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in candidates:
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            if _denied(rel):
                continue
            data = path.read_bytes()
            digest = None
            size = len(data)
            if size <= max_bytes:
                digest = "sha256:" + hashlib.sha256(data).hexdigest()
            entries.append(FileEntry(path=rel, kind="file", size=size, digest=digest))
    if not entries:
        return None
    return NativeStateManifest(
        provider_id=provider_id,
        native_id=native_id,
        lineage_id=lineage_id,
        files=tuple(entries),
        cli_version=cli_version,
    )


def validate_manifest(manifest: NativeStateManifest, root: Path) -> bool:
    """Every listed file exists with matching digest (oversized entries fail)."""
    for entry in manifest.files:
        path = root / entry.path
        if not path.is_file() or path.is_symlink():
            return False
        if entry.digest is None:
            return False
        actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != entry.digest:
            return False
    return True
