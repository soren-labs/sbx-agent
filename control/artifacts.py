"""Durable artifact packages (SOR-83/B1).

An *artifact* is the durable handoff product of a run: a manifest plus the
workspace files it produced, persisted outside the sandbox so reads and
downloads keep working after sandbox teardown.

Package format (frozen v1): ``patch`` — the changed-file set of the declared
workspace, content-addressed by sha256 in ``manifest.json``. Applying an
artifact overlays ``files/`` onto a checkout of ``base_sha``; per-file
checksums then verify the result (``ArtifactPackage.verify_dir``). A
caller-produced unified diff may be attached as the conventional
``patch.diff`` payload member. Git bundle is deliberately not the v1 path:
content addressing is deterministic, needs no git objects at rest, and
verifies without a repo.

Security boundary: collection is governed by ``WorkspacePolicy`` — an
allowlist of globs relative to the workspace root, minus an always-on
denylist (provider ``home/``, ``.git`` / ``.codex`` / ``.grok`` /
``.gemini`` config dirs, dotfile credentials, key material, runner
bookkeeping). Symlinks are never followed, so a symlink cannot smuggle a
denied file in under an allowed name. ``forbidden_values`` additionally
fails the whole build when a known secret appears inside an allowed file.
The store re-verifies every member against the manifest on write and on
read; corrupt or incomplete packages surface as ``ArtifactCorruptError``,
never as inferred content.

Listing (SOR-201): every store maintains a durable producer-agent ->
artifact index (sorted ``ArtifactIndexEntry`` rows) written on
``put``/``delete``. ``list_page`` serves keyset pages —
``(created_at, artifact_id)`` order, opaque ``cursor`` — and a filtered
``list``/``list_page`` reads one index document plus the page's
manifests, never a global scan. ``rebuild_index`` reconstructs the index
from stored manifests: the migration for pre-index data and the repair
path for index drift.

This module is the B1 core only: no workspace declaration, no API routes.
Integration lanes wire a collector over ``SandboxBackend.exec`` for remote
sandboxes and expose ``read`` as the download seam.
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from control.config import ARTIFACTS_DICT_NAME
from control.latency import observe

ARTIFACT_FORMAT_PATCH = "patch"
MANIFEST_MEMBER = "manifest.json"
PATCH_MEMBER = "patch.diff"  # conventional caller-supplied unified diff member
FILES_PREFIX = "files/"
SCHEMA_VERSION = 1

_ARTIFACT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

# Bounded concurrency for the Modal Dict member fan-out (SOR-118): enough
# to hide per-RPC latency without turning one request into a Dict burst.
_DICT_FANOUT = 8

# Listing/page fetch pools run hotter (SOR-199): a 500-manifest page at
# fanout 8 costs ~63 serial round-trips (~2.5s), over the Console's <2s
# principal-data budget. Reads carry no commit-order constraint, so they
# fan out wider than the write path.
_LIST_FANOUT = 32

# Listing page cache bounds — mirrors ``control.store``: TTL bounds the
# staleness of a page whose ``ver`` bump was lost mid-write; the STALE
# bound caps how long Dict read failures may be masked by a cached page.
_LIST_CACHE_TTL_S = float(os.environ.get("SBX_LIST_CACHE_TTL_S", "30"))
_LIST_CACHE_STALE_S = max(10 * _LIST_CACHE_TTL_S, 120.0)
_PAGE_CACHE_MAX = 64

# Sentinel for "the version token could not be read" — distinct from a
# stored ``None`` so a failed token read never looks like a write.
_VER_UNREAD: Any = object()

# Always-on denylist. Applied to every collection regardless of the
# caller's allowlist so provider credentials, auth stores and key material
# can never enter an artifact. ``_DENIED_COMPONENTS`` matches a path
# component at any depth; ``_DENIED_TOP_LEVEL`` only at the workspace root
# (a legit repo dir may be named ``src/home`` but ``home/`` at the root is
# the provider HOME restored from the credential blob). Runner bookkeeping
# (``session.json``, event streams, ``turns/``, ``inbox/``) is run evidence,
# not product — and the event stream can embed pasted user text — so it is
# denied at the root too.
_DENIED_COMPONENTS = frozenset(
    {".git", ".codex", ".grok", ".gemini", ".claude", ".ssh", ".gnupg", ".aws", ".sbx-handoff"}
)
_DENIED_TOP_LEVEL = frozenset(
    {
        "home",
        "turns",
        "inbox",
        ".config",
        ".local",
        ".docker",
        ".cache",
        ".npm",
        ".kube",
        "session.json",
        "events.jsonl",
        "events.raw.jsonl",
        "runner.pid",
    }
)
_DENIED_BASENAME_GLOBS = (
    ".env",
    ".env.*",
    ".netrc",
    "netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    ".dockercfg",
    "auth.json",
    "credentials*",
    ".credentials*",
    "events*.jsonl",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "*.jks",
)


class ArtifactError(Exception):
    """Base class for artifact package/store failures."""


class ArtifactNotFoundError(ArtifactError):
    """Artifact id or member does not exist."""


class ArtifactCorruptError(ArtifactError):
    """Stored manifest/member fails decode or checksum verification."""


class ArtifactSecretError(ArtifactError):
    """A forbidden value was found inside an otherwise-allowed file."""

    def __init__(self, paths: Sequence[str]) -> None:
        self.paths = sorted(paths)
        joined = ", ".join(self.paths)
        super().__init__(f"forbidden content found in workspace files: {joined}")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_artifact_id(artifact_id: Any) -> str:
    if not isinstance(artifact_id, str) or not _ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise ArtifactError(f"invalid artifact id {artifact_id!r}")
    return artifact_id


def _member_name_error(name: Any) -> str | None:
    """None when ``name`` is a safe store-relative member name."""
    if not isinstance(name, str) or not name or len(name) > 512:
        return f"invalid member name {name!r}"
    if "\\" in name or ":" in name or name.startswith("/"):
        return f"unsafe member name {name!r}"
    parts = PurePosixPath(name).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        return f"unsafe member name {name!r}"
    return None


def _relpath_error(relpath: Any) -> str | None:
    if not isinstance(relpath, str) or not relpath:
        return f"invalid path {relpath!r}"
    if "\\" in relpath or relpath.startswith("/"):
        return f"unsafe path {relpath!r}"
    parts = PurePosixPath(relpath).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        return f"unsafe path {relpath!r}"
    return None


def _sha256_ok(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class ArtifactFile:
    """One collected workspace file (path is workspace-relative posix)."""

    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class TestResult:
    """A test command run by the producer and its exit code."""

    __test__ = False  # not a pytest class
    command: str
    exit_code: int


@dataclass
class ArtifactManifest:
    """``manifest.json`` content. See module docstring for the format."""

    artifact_id: str
    base_sha: str = ""
    head_sha: str = ""
    repo: str = ""
    created_at: str = ""
    producer_agent_id: str = ""
    producer_run_id: str | None = None
    format: str = ARTIFACT_FORMAT_PATCH
    files: list[ArtifactFile] = field(default_factory=list)
    tests: list[TestResult] = field(default_factory=list)
    payloads: dict[str, str] = field(default_factory=dict)  # member name -> sha256
    warnings: list[str] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION


def manifest_to_dict(manifest: ArtifactManifest) -> dict[str, Any]:
    return {
        "schema_version": manifest.schema_version,
        "artifact_id": manifest.artifact_id,
        "format": manifest.format,
        "base_sha": manifest.base_sha,
        "head_sha": manifest.head_sha,
        "repo": manifest.repo,
        "created_at": manifest.created_at,
        "producer": {
            "agent_id": manifest.producer_agent_id,
            "run_id": manifest.producer_run_id,
        },
        "files": [{"path": f.path, "sha256": f.sha256, "size": f.size} for f in manifest.files],
        "tests": [{"command": t.command, "exit_code": t.exit_code} for t in manifest.tests],
        "payloads": dict(sorted(manifest.payloads.items())),
        "warnings": list(manifest.warnings),
    }


def manifest_from_dict(data: Any) -> ArtifactManifest:
    """Strict decode; raises ``ArtifactCorruptError`` on unusable payloads."""
    if not isinstance(data, dict):
        raise ArtifactCorruptError("manifest is not a dict")
    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise ArtifactCorruptError(f"unsupported manifest schema_version {version!r}")
    artifact_id = data.get("artifact_id")
    try:
        _validate_artifact_id(artifact_id)
    except ArtifactError as exc:
        raise ArtifactCorruptError(str(exc)) from None
    fmt = data.get("format")
    if fmt != ARTIFACT_FORMAT_PATCH:
        raise ArtifactCorruptError(f"unknown artifact format {fmt!r}")
    manifest = ArtifactManifest(artifact_id=str(artifact_id))
    for key in ("base_sha", "head_sha", "created_at", "repo"):
        value = data.get(key, "")
        if not isinstance(value, str):
            raise ArtifactCorruptError(f"manifest field {key} must be a string")
        setattr(manifest, key, value)
    producer = data.get("producer")
    if not isinstance(producer, dict) or not isinstance(producer.get("agent_id"), str):
        raise ArtifactCorruptError("manifest producer must be a dict with agent_id")
    manifest.producer_agent_id = producer["agent_id"]
    run_id = producer.get("run_id")
    if run_id is not None and not isinstance(run_id, str):
        raise ArtifactCorruptError("manifest producer.run_id must be a string or null")
    manifest.producer_run_id = run_id
    files = data.get("files", [])
    if not isinstance(files, list):
        raise ArtifactCorruptError("manifest files must be a list")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ArtifactCorruptError("manifest file entry must be a dict")
        path = entry.get("path")
        if _relpath_error(path) is not None or path in seen:
            raise ArtifactCorruptError(f"manifest file path invalid or duplicate: {path!r}")
        if not _sha256_ok(entry.get("sha256")):
            raise ArtifactCorruptError(f"manifest file {path!r} has invalid sha256")
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ArtifactCorruptError(f"manifest file {path!r} has invalid size")
        seen.add(str(path))
        manifest.files.append(ArtifactFile(str(path), str(entry["sha256"]), int(size)))
    tests = data.get("tests", [])
    if not isinstance(tests, list):
        raise ArtifactCorruptError("manifest tests must be a list")
    for entry in tests:
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
            raise ArtifactCorruptError("manifest test entry needs a command string")
        code = entry.get("exit_code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise ArtifactCorruptError("manifest test exit_code must be an int")
        manifest.tests.append(TestResult(entry["command"], code))
    payloads = data.get("payloads", {})
    if not isinstance(payloads, dict):
        raise ArtifactCorruptError("manifest payloads must be a dict")
    for name, digest in payloads.items():
        if _member_name_error(name) is not None or name == MANIFEST_MEMBER:
            raise ArtifactCorruptError(f"manifest payload name invalid: {name!r}")
        if name.startswith(FILES_PREFIX):
            raise ArtifactCorruptError(f"manifest payload {name!r} uses reserved prefix")
        if not _sha256_ok(digest):
            raise ArtifactCorruptError(f"manifest payload {name!r} has invalid sha256")
        manifest.payloads[str(name)] = str(digest)
    warnings = data.get("warnings", [])
    if not isinstance(warnings, list) or any(not isinstance(w, str) for w in warnings):
        raise ArtifactCorruptError("manifest warnings must be a string list")
    manifest.warnings = [str(w) for w in warnings]
    return manifest


def manifest_dumps(manifest: ArtifactManifest) -> bytes:
    """Canonical manifest bytes (sorted keys, stable across processes)."""
    text = json.dumps(manifest_to_dict(manifest), sort_keys=True, indent=2)
    return (text + "\n").encode("utf-8")


def manifest_loads(data: bytes | str) -> ArtifactManifest:
    try:
        raw = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ArtifactCorruptError(f"manifest is not valid json: {exc}") from None
    return manifest_from_dict(raw)


@dataclass(frozen=True)
class WorkspacePolicy:
    """Allowlist globs minus the always-on secret denylist.

    ``include`` patterns use ``fnmatch`` semantics on posix workspace-
    relative paths (``*`` crosses ``/``). ``forbidden_values`` are byte
    strings — typically a credential canary — that fail the build when
    found inside an allowed file.
    """

    include: tuple[str, ...] = ("*",)
    forbidden_values: tuple[bytes, ...] = ()

    def denial(self, relpath: str) -> str | None:
        """Reason the path is excluded, or None when it is not denied."""
        parts = PurePosixPath(relpath).parts
        if not parts:
            return "empty path"
        if parts[0] in _DENIED_TOP_LEVEL:
            return f"top-level {parts[0]} is excluded"
        for part in parts:
            if part in _DENIED_COMPONENTS:
                return f"{part}/ is excluded"
        base = parts[-1]
        for glob in _DENIED_BASENAME_GLOBS:
            if fnmatch.fnmatchcase(base, glob):
                return f"{base} matches secret pattern {glob}"
        return None

    def is_included(self, relpath: str) -> bool:
        return any(fnmatch.fnmatchcase(relpath, pat) for pat in self.include)

    def is_allowed(self, relpath: str) -> bool:
        return _relpath_error(relpath) is None and (
            self.denial(relpath) is None and self.is_included(relpath)
        )


def collect_workspace(
    workspace: Path | str,
    policy: WorkspacePolicy | None = None,
) -> tuple[list[ArtifactFile], dict[str, bytes], list[str]]:
    """Walk ``workspace`` and collect allowed regular files.

    Returns ``(files, members, warnings)`` where ``members`` maps
    ``files/<relpath>`` to content. Raises ``ArtifactSecretError`` when any
    collected file contains a forbidden value; denied paths are excluded
    before content is ever read. Symlinks are skipped (never followed), so
    a symlink cannot smuggle a denied file in under an allowed name.
    """
    root = Path(workspace)
    if not root.is_dir():
        raise ArtifactError(f"workspace {root} is not a directory")
    policy = policy or WorkspacePolicy()
    files: list[ArtifactFile] = []
    members: dict[str, bytes] = {}
    warnings: list[str] = []
    leaks: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if rel_dir == "." else f"{rel_dir}/"
        kept: list[str] = []
        for name in dirnames:
            if (Path(dirpath) / name).is_symlink():
                warnings.append(f"skipped symlinked dir {prefix}{name}")
            elif policy.denial(f"{prefix}{name}") is None:
                kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            relpath = f"{prefix}{name}"
            full = Path(dirpath) / name
            if full.is_symlink():
                warnings.append(f"skipped symlink {relpath}")
                continue
            if policy.denial(relpath) is not None or not policy.is_included(relpath):
                continue
            if not full.is_file():
                continue
            data = full.read_bytes()
            if any(value and value in data for value in policy.forbidden_values):
                leaks.append(relpath)
                continue
            files.append(ArtifactFile(relpath, sha256_hex(data), len(data)))
            members[f"{FILES_PREFIX}{relpath}"] = data
    if leaks:
        raise ArtifactSecretError(leaks)
    files.sort(key=lambda f: f.path)
    return files, members, warnings


def _declared_members(manifest: ArtifactManifest) -> dict[str, tuple[str, int | None]]:
    """Member name -> (sha256, size-or-None) for every manifest entry."""
    out: dict[str, tuple[str, int | None]] = {
        f"{FILES_PREFIX}{f.path}": (f.sha256, f.size) for f in manifest.files
    }
    for name, digest in manifest.payloads.items():
        out[name] = (digest, None)
    return out


def _verify_members(manifest: ArtifactManifest, members: Mapping[str, bytes]) -> None:
    """``members`` must exactly cover the manifest and match its checksums."""
    expected = _declared_members(manifest)
    for name in members:
        err = _member_name_error(name)
        if err is not None:
            raise ArtifactError(err)
        if name not in expected:
            raise ArtifactError(f"undeclared member {name!r}")
    missing = sorted(set(expected) - set(members))
    if missing:
        raise ArtifactCorruptError(f"artifact {manifest.artifact_id} missing members: {missing}")
    for name, data in members.items():
        digest, size = expected[name]
        if not isinstance(data, bytes):
            raise ArtifactCorruptError(f"member {name!r} is not bytes")
        if size is not None and len(data) != size:
            raise ArtifactCorruptError(f"member {name!r} size mismatch")
        if sha256_hex(data) != digest:
            raise ArtifactCorruptError(f"member {name!r} checksum mismatch")


def _read_declared(manifest: ArtifactManifest, member: str) -> tuple[str, int | None]:
    if member == MANIFEST_MEMBER:
        raise ArtifactError("manifest member is served as manifest.json, not a member")
    declared = _declared_members(manifest)
    if member not in declared:
        raise ArtifactNotFoundError(f"artifact {manifest.artifact_id} has no member {member!r}")
    return declared[member]


@dataclass
class ArtifactPackage:
    """An opened artifact: manifest plus integrity-verified member bytes."""

    manifest: ArtifactManifest
    members: dict[str, bytes]

    def member(self, name: str) -> bytes:
        if name == MANIFEST_MEMBER:
            return manifest_dumps(self.manifest)
        if name not in self.members:
            raise ArtifactNotFoundError(
                f"artifact {self.manifest.artifact_id} has no member {name!r}"
            )
        return self.members[name]

    def file_bytes(self, relpath: str) -> bytes:
        """Content of a collected workspace file."""
        return self.member(f"{FILES_PREFIX}{relpath}")

    def list_files(self) -> list[str]:
        return [f.path for f in self.manifest.files]

    def write_to(self, root: Path | str) -> list[str]:
        """Overlay ``files/`` onto ``root`` (a base_sha checkout). Returns
        the written workspace-relative paths."""
        base = Path(root)
        written: list[str] = []
        for entry in self.manifest.files:
            if _relpath_error(entry.path) is not None:
                raise ArtifactCorruptError(f"manifest path unsafe: {entry.path!r}")
            dest = base / entry.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self.members[f"{FILES_PREFIX}{entry.path}"])
            written.append(entry.path)
        return written

    def verify_dir(self, root: Path | str) -> list[str]:
        """Workspace-relative paths under ``root`` whose sha256 differs from
        the manifest (or are missing). Empty list = verified."""
        base = Path(root)
        bad: list[str] = []
        for entry in self.manifest.files:
            try:
                data = (base / entry.path).read_bytes()
            except OSError:
                bad.append(entry.path)
                continue
            if len(data) != entry.size or sha256_hex(data) != entry.sha256:
                bad.append(entry.path)
        return sorted(bad)


@dataclass(frozen=True)
class ArtifactIndexEntry:
    """One row of the durable producer-agent -> artifact index (SOR-201).

    Only listing metadata is stored — never member bytes or workspace
    content — so the index carries no secrets beyond what a manifest
    already exposes.
    """

    artifact_id: str
    created_at: str
    producer_run_id: str | None = None

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.created_at, self.artifact_id)


@dataclass(frozen=True)
class ArtifactPage:
    """One keyset page of a listing (SOR-201)."""

    artifacts: tuple[ArtifactManifest, ...]
    next_cursor: str | None


_INDEX_FORMAT = 1


def _index_entry(manifest: ArtifactManifest) -> ArtifactIndexEntry:
    return ArtifactIndexEntry(manifest.artifact_id, manifest.created_at, manifest.producer_run_id)


def _index_add_entry(
    entries: Sequence[ArtifactIndexEntry], entry: ArtifactIndexEntry
) -> list[ArtifactIndexEntry]:
    """Sorted index rows with ``entry`` inserted (same artifact_id replaces)."""
    kept = [e for e in entries if e.artifact_id != entry.artifact_id]
    kept.append(entry)
    kept.sort(key=lambda e: e.sort_key)
    return kept


def _index_dumps(entries: Sequence[ArtifactIndexEntry]) -> bytes:
    """Canonical bytes for one agent's index document."""
    body = {
        "v": _INDEX_FORMAT,
        "entries": [
            {"a": e.artifact_id, "t": e.created_at, "r": e.producer_run_id} for e in entries
        ],
    }
    return (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _index_loads(raw: Any) -> list[ArtifactIndexEntry]:
    """Tolerant decode of one index document; unusable payloads read empty."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, bytes):
        return []
    try:
        body = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(body, dict) or body.get("v") != _INDEX_FORMAT:
        return []
    items = body.get("entries")
    if not isinstance(items, list):
        return []
    out: list[ArtifactIndexEntry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        artifact_id, created_at = item.get("a"), item.get("t")
        if not isinstance(artifact_id, str) or not isinstance(created_at, str):
            continue
        run_id = item.get("r")
        out.append(
            ArtifactIndexEntry(artifact_id, created_at, run_id if isinstance(run_id, str) else None)
        )
    out.sort(key=lambda e: e.sort_key)
    return out


def _encode_cursor(entry: ArtifactIndexEntry) -> str:
    """Opaque keyset cursor over (created_at, artifact_id)."""
    payload = json.dumps([entry.created_at, entry.artifact_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    """``(created_at, artifact_id)`` encoded by ``_encode_cursor``."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        if isinstance(data, list) and len(data) == 2 and all(isinstance(v, str) for v in data):
            return data[0], data[1]
    except (binascii.Error, ValueError):
        pass
    raise ArtifactError(f"malformed artifact cursor {cursor!r}")


def _page_from_entries(
    entries: Sequence[ArtifactIndexEntry],
    *,
    cursor: str | None,
    limit: int | None,
    fetch: Callable[[str], ArtifactManifest | None],
    drop: Callable[[str], None] | None = None,
) -> ArtifactPage:
    """Keyset page over sorted index rows.

    ``fetch`` decodes one manifest, or returns ``None`` when the row is
    stale (package deleted, corrupt, or re-owned by another producer);
    stale rows are dropped from the durable index via ``drop`` rather
    than served.
    """
    if limit is not None and limit < 1:
        raise ArtifactError(f"limit must be a positive int, got {limit!r}")
    after = _decode_cursor(cursor) if cursor else None
    out: list[ArtifactManifest] = []
    remaining = False
    for entry in entries:
        if after is not None and entry.sort_key <= after:
            continue
        if limit is not None and len(out) >= limit:
            remaining = True
            break
        manifest = fetch(entry.artifact_id)
        if manifest is None:
            if drop is not None:
                drop(entry.artifact_id)
            continue
        out.append(manifest)
    next_cursor = _encode_cursor(_index_entry(out[-1])) if remaining and out else None
    return ArtifactPage(tuple(out), next_cursor)


def _page_from_entries_par(
    entries: Sequence[ArtifactIndexEntry],
    *,
    cursor: str | None,
    limit: int | None,
    fetch: Callable[[str], ArtifactManifest | None],
    drop: Callable[[str], None] | None = None,
    window: int = 128,
) -> ArtifactPage:
    """``_page_from_entries`` with bounded parallel manifest fetches.

    Global listings page over thousands of index rows; a serial ``fetch``
    per row costs one Dict round-trip each. Windows keep the fan-out
    bounded while preserving ``limit``-over-valid-rows semantics — stale
    rows are dropped and never consume the page budget.
    """
    if limit is not None and limit < 1:
        raise ArtifactError(f"limit must be a positive int, got {limit!r}")
    after = _decode_cursor(cursor) if cursor else None
    pending = [e for e in entries if after is None or e.sort_key > after]
    out: list[ArtifactManifest] = []
    remaining = False
    pos = 0
    while pos < len(pending):
        if limit is not None and len(out) >= limit:
            remaining = True
            break
        width = window if limit is None else min(window, max(1, limit - len(out)))
        chunk = pending[pos : pos + width]
        pos += len(chunk)
        with ThreadPoolExecutor(max_workers=_LIST_FANOUT) as pool:
            manifests = list(pool.map(fetch, (e.artifact_id for e in chunk)))
        for entry, manifest in zip(chunk, manifests):
            if limit is not None and len(out) >= limit:
                remaining = True
                break
            if manifest is None:
                if drop is not None:
                    drop(entry.artifact_id)
                continue
            out.append(manifest)
        if remaining:
            break
    next_cursor = _encode_cursor(_index_entry(out[-1])) if remaining and out else None
    return ArtifactPage(tuple(out), next_cursor)


def page_manifests(
    manifests: Sequence[ArtifactManifest],
    *,
    cursor: str | None = None,
    limit: int | None = None,
) -> ArtifactPage:
    """Keyset page over already-sorted manifests (created_at, artifact_id).

    The pagination surface for callers that hold full result sets —
    e.g. stores where listing is inherently unfiltered/global.
    """
    return _page_from_entries(
        [_index_entry(m) for m in manifests],
        cursor=cursor,
        limit=limit,
        fetch=lambda aid: next((m for m in manifests if m.artifact_id == aid), None),
    )


@runtime_checkable
class ArtifactStore(Protocol):
    """Persistence for artifact packages, keyed by artifact_id.

    Implementations must verify member integrity against the manifest on
    ``put`` and ``open``/``read`` — a store that persists mismatched bytes
    silently is a corruption amplifier.

    SOR-201: stores also maintain a durable producer-agent -> artifact
    index so single-agent listing/pagination never scans every historical
    artifact. ``rebuild_index`` reconstructs it from stored manifests —
    the safe migration for pre-index data and the repair path for index
    drift (e.g. lost read-modify-write under concurrent writers).
    """

    def put(self, manifest: ArtifactManifest, members: Mapping[str, bytes]) -> ArtifactManifest:
        """Validate + persist. Returns the manifest."""

    def manifest(self, artifact_id: str) -> ArtifactManifest:
        """Decoded manifest. ``ArtifactNotFoundError`` / ``ArtifactCorruptError``."""

    def open(self, artifact_id: str) -> ArtifactPackage:
        """Manifest + all declared members, checksums verified."""

    def read(self, artifact_id: str, member: str) -> bytes:
        """One member's bytes (``manifest.json`` serves canonical manifest
        bytes). The teardown-independent download seam."""

    def list(self, *, agent_id: str | None = None) -> list[ArtifactManifest]:
        """Decodable manifests, optionally filtered by producer agent."""

    def list_page(
        self,
        *,
        agent_id: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ArtifactPage:
        """Keyset page of manifests (created_at, artifact_id order)."""

    def rebuild_index(self) -> int:
        """Rebuild the per-agent index from stored manifests; returns the
        number of index rows written."""

    def delete(self, artifact_id: str) -> None:
        """Remove the package; missing ids are ignored."""


def _package_from(manifest: ArtifactManifest, members: dict[str, bytes]) -> ArtifactPackage:
    try:
        _verify_members(manifest, members)
    except ArtifactError as exc:
        if isinstance(exc, ArtifactCorruptError):
            raise
        raise ArtifactCorruptError(str(exc)) from None
    return ArtifactPackage(manifest, members)


class InMemoryArtifactStore:
    """Dict-backed store for tests and ephemeral deployments.

    Manifests are kept as canonical bytes so corrupt-payload tests exercise
    the same decode path as the file store.
    """

    def __init__(self) -> None:
        self._items: dict[str, tuple[bytes, dict[str, bytes]]] = {}
        # SOR-201: producer-agent -> sorted index rows; maintained on
        # put/delete, lazily rebuilt on the first filtered query.
        self._index: dict[str, list[ArtifactIndexEntry]] = {}
        self._index_ready = False
        self._lock = threading.Lock()

    def _index_remove_row(self, artifact_id: str, agent_id: str | None) -> None:
        agents = (agent_id,) if agent_id is not None else tuple(self._index)
        for agent in agents:
            rows = self._index.get(agent)
            if rows is None:
                continue
            kept = [e for e in rows if e.artifact_id != artifact_id]
            if len(kept) != len(rows):
                if kept:
                    self._index[agent] = kept
                else:
                    self._index.pop(agent, None)

    def _index_update_put(self, manifest: ArtifactManifest, old_agent: str | None) -> None:
        if old_agent is not None and old_agent != manifest.producer_agent_id:
            self._index_remove_row(manifest.artifact_id, old_agent)
        self._index[manifest.producer_agent_id] = _index_add_entry(
            self._index.get(manifest.producer_agent_id, []), _index_entry(manifest)
        )

    def _ensure_index(self) -> None:
        if not self._index_ready:
            self.rebuild_index()

    def rebuild_index(self) -> int:
        """Rescan every stored manifest and rewrite all agent index rows."""
        with self._lock:
            groups: dict[str, list[ArtifactIndexEntry]] = {}
            for raw, _members in self._items.values():
                try:
                    manifest = manifest_loads(raw)
                except ArtifactError:
                    continue
                groups.setdefault(manifest.producer_agent_id, []).append(_index_entry(manifest))
            self._index = {
                agent: sorted(rows, key=lambda e: e.sort_key) for agent, rows in groups.items()
            }
            self._index_ready = True
            return sum(len(rows) for rows in groups.values())

    def put(self, manifest: ArtifactManifest, members: Mapping[str, bytes]) -> ArtifactManifest:
        _validate_artifact_id(manifest.artifact_id)
        _verify_members(manifest, members)
        with self._lock:
            old_agent: str | None = None
            existing = self._items.get(manifest.artifact_id)
            if existing is not None:
                try:
                    old_agent = manifest_loads(existing[0]).producer_agent_id
                except ArtifactError:
                    old_agent = None
            self._items[manifest.artifact_id] = (
                manifest_dumps(manifest),
                dict(members),
            )
            self._index_update_put(manifest, old_agent)
        return manifest

    def _entry(self, artifact_id: str) -> tuple[bytes, dict[str, bytes]]:
        _validate_artifact_id(artifact_id)
        with self._lock:
            entry = self._items.get(artifact_id)
        if entry is None:
            raise ArtifactNotFoundError(f"unknown artifact {artifact_id!r}")
        return entry

    def manifest(self, artifact_id: str) -> ArtifactManifest:
        raw, _ = self._entry(artifact_id)
        decoded = manifest_loads(raw)
        if decoded.artifact_id != artifact_id:
            raise ArtifactCorruptError(
                f"stored manifest id {decoded.artifact_id!r} != {artifact_id!r}"
            )
        return decoded

    def open(self, artifact_id: str) -> ArtifactPackage:
        manifest = self.manifest(artifact_id)
        _, members = self._entry(artifact_id)
        declared = _declared_members(manifest)
        kept = {name: members[name] for name in declared if name in members}
        return _package_from(manifest, kept)

    def read(self, artifact_id: str, member: str) -> bytes:
        if member == MANIFEST_MEMBER:
            return manifest_dumps(self.manifest(artifact_id))
        manifest = self.manifest(artifact_id)
        digest, size = _read_declared(manifest, member)
        _, members = self._entry(artifact_id)
        data = members.get(member)
        if data is None:
            raise ArtifactCorruptError(f"member {member!r} missing from store")
        if size is not None and len(data) != size:
            raise ArtifactCorruptError(f"member {member!r} size mismatch")
        if sha256_hex(data) != digest:
            raise ArtifactCorruptError(f"member {member!r} checksum mismatch")
        return data

    def _fetch_for_agent(self, agent_id: str) -> Callable[[str], ArtifactManifest | None]:
        def fetch(artifact_id: str) -> ArtifactManifest | None:
            try:
                manifest = self.manifest(artifact_id)
            except ArtifactError:
                return None
            # Index row from before a producer re-assignment is stale.
            return manifest if manifest.producer_agent_id == agent_id else None

        return fetch

    def _index_drop_row(self, agent_id: str, artifact_id: str) -> None:
        with self._lock:
            self._index_remove_row(artifact_id, agent_id)

    def list(self, *, agent_id: str | None = None) -> list[ArtifactManifest]:
        if agent_id is not None:
            page = self.list_page(agent_id=agent_id)
            return list(page.artifacts)
        with self._lock:
            keys = list(self._items)
        out: list[ArtifactManifest] = []
        for key in keys:
            try:
                manifest = self.manifest(key)
            except ArtifactError:
                continue
            out.append(manifest)
        return sorted(out, key=lambda m: (m.created_at, m.artifact_id))

    def list_page(
        self,
        *,
        agent_id: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ArtifactPage:
        if agent_id is not None:
            self._ensure_index()
            with self._lock:
                entries = list(self._index.get(agent_id, []))
            return _page_from_entries(
                entries,
                cursor=cursor,
                limit=limit,
                fetch=self._fetch_for_agent(agent_id),
                drop=lambda aid: self._index_drop_row(agent_id, aid),
            )
        return page_manifests(self.list(), cursor=cursor, limit=limit)

    def delete(self, artifact_id: str) -> None:
        with self._lock:
            agent_hint: str | None = None
            existing = self._items.get(artifact_id)
            if existing is not None:
                try:
                    agent_hint = manifest_loads(existing[0]).producer_agent_id
                except ArtifactError:
                    agent_hint = None
            self._items.pop(artifact_id, None)
            self._index_remove_row(artifact_id, agent_hint)


class FileArtifactStore:
    """Local durable store: ``<root>/<artifact_id>/manifest.json`` plus
    ``members/<name>``.

    Writes are staged in a temp dir and atomically renamed into place so a
    crash mid-write cannot leave a half-written package. ``root`` must live
    outside the sandbox workdir — the store outlives sandbox teardown by
    construction.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._index_ready = False
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root

    def _dir(self, artifact_id: str) -> Path:
        return self._root / _validate_artifact_id(artifact_id)

    # ---- SOR-201 producer-agent index -------------------------------------
    # Layout: ``<root>/.index/<sha256(agent_id)>.json`` (one sorted doc per
    # agent) plus ``<root>/.index/_built`` once the index covers the store.
    # Dot-prefixed, so package enumeration in ``list`` never sees it; agent
    # ids are hashed into filenames so arbitrary ids stay filesystem-safe.

    def _index_dir(self) -> Path:
        return self._root / ".index"

    def _index_path(self, agent_id: str) -> Path:
        return self._index_dir() / f"{sha256_hex(agent_id.encode('utf-8'))}.json"

    def _index_marker(self) -> Path:
        return self._index_dir() / "_built"

    def _load_index(self, agent_id: str) -> list[ArtifactIndexEntry]:
        try:
            raw = self._index_path(agent_id).read_bytes()
        except OSError:
            return []
        return _index_loads(raw)

    def _write_index(self, agent_id: str, rows: list[ArtifactIndexEntry]) -> None:
        """Atomically persist one agent's index doc (empty removes it)."""
        path = self._index_path(agent_id)
        if not rows:
            path.unlink(missing_ok=True)
            return
        self._index_dir().mkdir(parents=True, exist_ok=True)
        staging = self._index_dir() / f".tmp-{uuid.uuid4().hex}"
        staging.write_bytes(_index_dumps(rows))
        os.replace(staging, path)

    def _index_add(self, manifest: ArtifactManifest, old_agent: str | None) -> None:
        """Maintain index rows for one write; caller holds ``_lock``."""
        if old_agent is not None and old_agent != manifest.producer_agent_id:
            self._index_remove_row(manifest.artifact_id, old_agent)
        agent = manifest.producer_agent_id
        self._write_index(agent, _index_add_entry(self._load_index(agent), _index_entry(manifest)))

    def _index_remove_row(self, artifact_id: str, agent_id: str | None) -> None:
        """Drop ``artifact_id`` from the index; caller holds ``_lock``.

        ``agent_id`` narrows the write to one doc; without it (e.g. the
        manifest was already corrupt) every agent doc is scanned — bounded
        by agent count, not artifact count.
        """
        if agent_id is not None:
            rows = self._load_index(agent_id)
            kept = [e for e in rows if e.artifact_id != artifact_id]
            if len(kept) != len(rows):
                self._write_index(agent_id, kept)
            return
        index_dir = self._index_dir()
        try:
            files = list(index_dir.iterdir())
        except OSError:
            return
        for path in files:
            if path.suffix != ".json":
                continue
            rows = _index_loads(path.read_bytes())
            kept = [e for e in rows if e.artifact_id != artifact_id]
            if len(kept) != len(rows):
                if kept:
                    staging = index_dir / f".tmp-{uuid.uuid4().hex}"
                    staging.write_bytes(_index_dumps(kept))
                    os.replace(staging, path)
                else:
                    path.unlink(missing_ok=True)

    def _ensure_index(self) -> None:
        if not self._index_ready and not self._index_marker().exists():
            self.rebuild_index()
        self._index_ready = True

    def rebuild_index(self) -> int:
        """Rescan every stored manifest and rewrite all agent index docs.

        Idempotent and safe to run on a live store: it only touches
        ``.index/`` files, and re-running converges the index to the
        current package set.
        """
        groups: dict[str, list[ArtifactIndexEntry]] = {}
        try:
            entries = sorted(self._root.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            try:
                manifest = self.manifest(entry.name)
            except ArtifactError:
                continue
            groups.setdefault(manifest.producer_agent_id, []).append(_index_entry(manifest))
        with self._lock:
            self._index_dir().mkdir(parents=True, exist_ok=True)
            keep = {f"{sha256_hex(agent.encode('utf-8'))}.json" for agent in groups}
            for path in self._index_dir().iterdir():
                if path.suffix == ".json" and path.name not in keep:
                    path.unlink(missing_ok=True)
            for agent, rows in groups.items():
                self._write_index(agent, sorted(rows, key=lambda e: e.sort_key))
            self._index_marker().write_bytes(b"1\n")
            self._index_ready = True
        return sum(len(rows) for rows in groups.values())

    def put(self, manifest: ArtifactManifest, members: Mapping[str, bytes]) -> ArtifactManifest:
        _validate_artifact_id(manifest.artifact_id)
        _verify_members(manifest, members)
        target = self._dir(manifest.artifact_id)
        old_agent: str | None = None
        try:
            old_agent = self.manifest(manifest.artifact_id).producer_agent_id
        except ArtifactError:
            old_agent = None
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(dir=self._root, prefix=".staging-"))
            try:
                for name, data in members.items():
                    dest = staging / "members" / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                (staging / MANIFEST_MEMBER).write_bytes(manifest_dumps(manifest))
                if target.exists():
                    trash = self._root / f".trash-{uuid.uuid4().hex}"
                    os.replace(target, trash)
                    try:
                        os.replace(staging, target)
                    except Exception:
                        os.replace(trash, target)  # restore the old package
                        raise
                    else:
                        shutil.rmtree(trash, ignore_errors=True)
                else:
                    os.replace(staging, target)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            self._index_add(manifest, old_agent)
        return manifest

    def _member_path(self, artifact_id: str, member: str) -> Path:
        return self._dir(artifact_id) / "members" / member

    def manifest(self, artifact_id: str) -> ArtifactManifest:
        path = self._dir(artifact_id) / MANIFEST_MEMBER
        try:
            raw = path.read_bytes()
        except OSError:
            raise ArtifactNotFoundError(f"unknown artifact {artifact_id!r}") from None
        decoded = manifest_loads(raw)
        if decoded.artifact_id != artifact_id:
            raise ArtifactCorruptError(
                f"stored manifest id {decoded.artifact_id!r} != {artifact_id!r}"
            )
        return decoded

    def open(self, artifact_id: str) -> ArtifactPackage:
        manifest = self.manifest(artifact_id)
        members: dict[str, bytes] = {}
        for name in _declared_members(manifest):
            try:
                members[name] = self._member_path(artifact_id, name).read_bytes()
            except OSError:
                raise ArtifactCorruptError(
                    f"artifact {artifact_id} member {name!r} missing from store"
                ) from None
        return _package_from(manifest, members)

    def read(self, artifact_id: str, member: str) -> bytes:
        if member == MANIFEST_MEMBER:
            return manifest_dumps(self.manifest(artifact_id))
        manifest = self.manifest(artifact_id)
        digest, size = _read_declared(manifest, member)
        try:
            data = self._member_path(artifact_id, member).read_bytes()
        except OSError:
            raise ArtifactCorruptError(
                f"artifact {artifact_id} member {member!r} missing from store"
            ) from None
        if size is not None and len(data) != size:
            raise ArtifactCorruptError(f"member {member!r} size mismatch")
        if sha256_hex(data) != digest:
            raise ArtifactCorruptError(f"member {member!r} checksum mismatch")
        return data

    def _fetch_for_agent(self, agent_id: str) -> Callable[[str], ArtifactManifest | None]:
        def fetch(artifact_id: str) -> ArtifactManifest | None:
            try:
                manifest = self.manifest(artifact_id)
            except ArtifactError:
                return None
            return manifest if manifest.producer_agent_id == agent_id else None

        return fetch

    def _index_prune(self, agent_id: str, artifact_id: str) -> None:
        with self._lock:
            self._index_remove_row(artifact_id, agent_id)

    def list(self, *, agent_id: str | None = None) -> list[ArtifactManifest]:
        if agent_id is not None:
            return list(self.list_page(agent_id=agent_id).artifacts)
        try:
            entries = sorted(self._root.iterdir())
        except OSError:
            return []
        out: list[ArtifactManifest] = []
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            try:
                manifest = self.manifest(entry.name)
            except ArtifactError:
                continue
            out.append(manifest)
        return sorted(out, key=lambda m: (m.created_at, m.artifact_id))

    def list_page(
        self,
        *,
        agent_id: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ArtifactPage:
        if agent_id is not None:
            # Index path: O(agent's artifacts), never scans every package.
            self._ensure_index()
            entries = self._load_index(agent_id)
            return _page_from_entries(
                entries,
                cursor=cursor,
                limit=limit,
                fetch=self._fetch_for_agent(agent_id),
                drop=lambda aid: self._index_prune(agent_id, aid),
            )
        return page_manifests(self.list(), cursor=cursor, limit=limit)

    def delete(self, artifact_id: str) -> None:
        _validate_artifact_id(artifact_id)
        agent_hint: str | None = None
        try:
            agent_hint = self.manifest(artifact_id).producer_agent_id
        except ArtifactError:
            agent_hint = None
        with self._lock:
            shutil.rmtree(self._dir(artifact_id), ignore_errors=True)
            self._index_remove_row(artifact_id, agent_hint)


class ModalDictArtifactStore:
    """Production store backed by ``modal.Dict``. Lazy-imports modal.

    Keys: ``<id>/manifest`` -> canonical manifest bytes, ``<id>/members``
    -> member name list, ``<id>/member/<name>`` -> bytes.

    SOR-201 index keys: ``index/agent/<sha256(agent_id)>`` ->
    one JSON doc of sorted ``ArtifactIndexEntry`` rows, and ``index/_built``
    once the index covers the whole Dict. ``modal.Dict`` has no prefix
    scan, so single-agent listing reads exactly one index document plus
    the page's manifests — never ``keys()`` over the global store. The
    doc is read-modify-write on ``put``/``delete`` under the process
    lock; a lost update is repaired by ``rebuild_index`` (or lazily on
    the first filtered query of a pre-index Dict).

    SOR-199 global index: ``index/global/<seq>`` -> chunked JSON docs of
    sorted ``ArtifactIndexEntry`` rows (``_GLOBAL_CHUNK_MAX`` per chunk)
    and ``index/_global_meta`` -> ``{"chunks": n}``, ``index/_global_built``
    once the index covers the whole Dict. Unfiltered ``list_page``/``list``
    read the chunk docs and fetch the page's manifests through a bounded
    pool — never ``keys()``/``items()`` over the global store, whose
    server-paged enumeration costs ~one round-trip per key (~85s at 11k
    keys). A lost index update is repaired by ``rebuild_global_index``.
    """

    _IDX_AGENT_PREFIX = "index/agent/"
    _IDX_BUILT = "index/_built"
    _IDX_GLOBAL_PREFIX = "index/global/"
    _IDX_GLOBAL_META = "index/_global_meta"
    _IDX_GLOBAL_BUILT = "index/_global_built"
    _IDX_GLOBAL_VER = "index/_global_ver"
    _GLOBAL_CHUNK_MAX = 2000

    def __init__(self, name: str = ARTIFACTS_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None
        self._lock = threading.Lock()
        self._index_ready = False
        self._global_ready = False
        self._build_lock = threading.Lock()
        # ``(cursor, limit) -> (ver, monotonic-ts, ArtifactPage)`` for the
        # unfiltered listing: a page fetch costs ~limit/fanout serial
        # round-trips, so repeat navigations serve from the page cache
        # keyed on the global ``ver`` token (same pattern as
        # ``ModalDictStore._list_cache``).
        self._page_cache: dict[tuple[Any, Any], tuple[Any, float, ArtifactPage]] = {}
        self._refresh_lock = threading.Lock()
        self._refresh_thread: threading.Thread | None = None

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    @staticmethod
    def _idx_key(agent_id: str) -> str:
        return f"{ModalDictArtifactStore._IDX_AGENT_PREFIX}{sha256_hex(agent_id.encode('utf-8'))}"

    @staticmethod
    def _mkey(artifact_id: str, member: str) -> str:
        return f"{artifact_id}/member/{member}"

    def _get(self, key: str) -> Any:
        with observe("modal_dict.get", store=self._name, key=key):
            return self._d().get(key)

    def _put(self, key: str, value: Any) -> None:
        with observe("modal_dict.put", store=self._name, key=key):
            self._d().put(key, value)

    def _pop(self, key: str) -> None:
        try:
            with observe("modal_dict.pop", store=self._name, key=key):
                self._d().pop(key)
        except KeyError:
            pass

    def put(self, manifest: ArtifactManifest, members: Mapping[str, bytes]) -> ArtifactManifest:
        _validate_artifact_id(manifest.artifact_id)
        _verify_members(manifest, members)
        aid = manifest.artifact_id
        # One Dict RPC per member serialized artifact create behind N network
        # round-trips (SOR-118); a bounded pool keeps the commit order —
        # manifest last — while the member fan-out runs concurrently.
        old_agent: str | None = None
        try:
            old_agent = self.manifest(aid).producer_agent_id
        except ArtifactError:
            old_agent = None
        with observe(
            "modal_dict.put_members",
            store=self._name,
            key=aid,
            members=len(members),
            bytes=sum(len(data) for data in members.values()),
        ):
            with ThreadPoolExecutor(max_workers=_DICT_FANOUT) as pool:
                futures = [
                    pool.submit(self._d().put, self._mkey(aid, name), data)
                    for name, data in members.items()
                ]
                for future in futures:
                    future.result()
        self._put(f"{aid}/members", sorted(members))
        self._put(f"{aid}/manifest", manifest_dumps(manifest))
        self._index_add(manifest, old_agent)
        try:
            self._global_index_add(manifest)
            self._global_ver_bump()
        except Exception:
            self._global_index_broken()
        return manifest

    def _index_add(self, manifest: ArtifactManifest, old_agent: str | None) -> None:
        """RMW the producer's index doc under the process lock."""
        with self._lock:
            if old_agent is not None and old_agent != manifest.producer_agent_id:
                self._index_remove_row(manifest.artifact_id, old_agent)
            key = self._idx_key(manifest.producer_agent_id)
            rows = _index_loads(self._get(key))
            self._put(key, _index_dumps(_index_add_entry(rows, _index_entry(manifest))))

    def _index_remove_row(self, artifact_id: str, agent_id: str | None) -> None:
        """Drop ``artifact_id`` from the index; caller holds ``_lock``.

        With ``agent_id`` this is one doc RMW; without it (a corrupt
        manifest hides the producer) every ``index/agent/*`` doc is
        scanned — bounded by agent count, and only on the rare
        undecodable-delete path.
        """
        if agent_id is not None:
            keys = [self._idx_key(agent_id)]
        else:
            keys = [
                k
                for k in self._d().keys()
                if isinstance(k, str) and k.startswith(self._IDX_AGENT_PREFIX)
            ]
        for key in keys:
            rows = _index_loads(self._get(key))
            kept = [e for e in rows if e.artifact_id != artifact_id]
            if len(kept) == len(rows):
                continue
            if kept:
                self._put(key, _index_dumps(kept))
            else:
                self._pop(key)

    def _ensure_index(self) -> None:
        """Lazily build the index on the first filtered query — the safe
        migration for Dicts that predate the index. ``_build_lock``
        double-checks so concurrent first queries share one rebuild."""
        if self._index_ready:
            return
        with self._build_lock:
            if self._index_ready:
                return
            if self._get(self._IDX_BUILT) is None:
                self.rebuild_index()
            self._index_ready = True

    def rebuild_index(self) -> int:
        """Rescan every manifest in the Dict and rewrite all agent docs.

        One ``keys()`` enumeration plus one point get per artifact — the
        only path that still touches global history, and it converges the
        index (drops docs for producers with no artifacts left).
        """
        artifact_ids = self._all_manifest_ids()
        with observe("modal_dict.get_manifests", store=self._name, manifests=len(artifact_ids)):
            with ThreadPoolExecutor(max_workers=_LIST_FANOUT) as pool:
                raws = list(pool.map(lambda aid: self._d().get(f"{aid}/manifest"), artifact_ids))
        groups: dict[str, list[ArtifactIndexEntry]] = {}
        for artifact_id, raw in zip(artifact_ids, raws):
            if raw is None:
                continue
            try:
                manifest = manifest_loads(raw)
            except ArtifactError:
                continue
            if manifest.artifact_id != artifact_id:
                continue
            groups.setdefault(manifest.producer_agent_id, []).append(_index_entry(manifest))
        keep = {self._idx_key(agent) for agent in groups}
        with observe("modal_dict.write_index", store=self._name, agents=len(groups)):
            existing = [
                k
                for k in self._d().keys()
                if isinstance(k, str) and k.startswith(self._IDX_AGENT_PREFIX)
            ]
            for key in existing:
                if key not in keep:
                    self._pop(key)
            for agent, rows in groups.items():
                self._put(self._idx_key(agent), _index_dumps(rows))
            self._put(self._IDX_BUILT, b"1")
        self._index_ready = True
        return sum(len(rows) for rows in groups.values())

    def _all_manifest_ids(self) -> list[str]:
        with observe("modal_dict.keys", store=self._name):
            keys = list(self._d().keys())
        # Artifact ids never contain "/", so ``<id>/manifest`` has exactly
        # two segments; the ``/`` check also drops ``<id>/member/manifest``
        # and every ``index/`` key.
        return [
            key[: -len("/manifest")]
            for key in keys
            if isinstance(key, str)
            and key.endswith("/manifest")
            and "/" not in key[: -len("/manifest")]
        ]

    def manifest(self, artifact_id: str) -> ArtifactManifest:
        raw = self._get(f"{artifact_id}/manifest")
        if raw is None:
            raise ArtifactNotFoundError(f"unknown artifact {artifact_id!r}")
        decoded = manifest_loads(raw)
        if decoded.artifact_id != artifact_id:
            raise ArtifactCorruptError(
                f"stored manifest id {decoded.artifact_id!r} != {artifact_id!r}"
            )
        return decoded

    def open(self, artifact_id: str) -> ArtifactPackage:
        manifest = self.manifest(artifact_id)
        names = list(_declared_members(manifest))
        with observe(
            "modal_dict.get_members", store=self._name, key=artifact_id, members=len(names)
        ):
            with ThreadPoolExecutor(max_workers=_DICT_FANOUT) as pool:
                datas = list(
                    pool.map(lambda name: self._d().get(self._mkey(artifact_id, name)), names)
                )
        members: dict[str, bytes] = {}
        for name, data in zip(names, datas):
            if data is None:
                raise ArtifactCorruptError(
                    f"artifact {artifact_id} member {name!r} missing from store"
                )
            members[name] = data
        return _package_from(manifest, members)

    def read(self, artifact_id: str, member: str) -> bytes:
        if member == MANIFEST_MEMBER:
            return manifest_dumps(self.manifest(artifact_id))
        manifest = self.manifest(artifact_id)
        digest, size = _read_declared(manifest, member)
        data = self._get(self._mkey(artifact_id, member))
        if data is None:
            raise ArtifactCorruptError(f"member {member!r} missing from store")
        if size is not None and len(data) != size:
            raise ArtifactCorruptError(f"member {member!r} size mismatch")
        if sha256_hex(data) != digest:
            raise ArtifactCorruptError(f"member {member!r} checksum mismatch")
        return data

    def _fetch_for_agent(self, agent_id: str) -> Callable[[str], ArtifactManifest | None]:
        def fetch(artifact_id: str) -> ArtifactManifest | None:
            try:
                manifest = self.manifest(artifact_id)
            except ArtifactError:
                return None
            return manifest if manifest.producer_agent_id == agent_id else None

        return fetch

    def _index_prune(self, agent_id: str, artifact_id: str) -> None:
        with self._lock:
            self._index_remove_row(artifact_id, agent_id)

    def _list_scan(self) -> list[ArtifactManifest]:
        """Honest full enumeration: ``keys()`` then point manifest gets.

        ``items()`` streams every value in the Dict — all member blobs —
        so one list call used to download the entire store and
        materialize it in memory. ``keys()`` carries no values. This is
        the degraded-path fallback and the rebuild source; steady-state
        reads go through the chunked global index (SOR-199).
        """
        artifact_ids = self._all_manifest_ids()
        with observe("modal_dict.get_manifests", store=self._name, manifests=len(artifact_ids)):
            with ThreadPoolExecutor(max_workers=_LIST_FANOUT) as pool:
                raws = list(pool.map(lambda aid: self._d().get(f"{aid}/manifest"), artifact_ids))
        out: list[ArtifactManifest] = []
        for artifact_id, raw in zip(artifact_ids, raws):
            if raw is None:
                continue
            try:
                manifest = manifest_loads(raw)
            except ArtifactError:
                continue
            if manifest.artifact_id != artifact_id:
                continue
            out.append(manifest)
        return sorted(out, key=lambda m: (m.created_at, m.artifact_id))

    def list(self, *, agent_id: str | None = None) -> list[ArtifactManifest]:
        if agent_id is not None:
            return list(self.list_page(agent_id=agent_id).artifacts)
        try:
            self._ensure_global_index()
            entries = self._global_entries()
            with observe("modal_dict.get_manifests", store=self._name, manifests=len(entries)):
                with ThreadPoolExecutor(max_workers=_LIST_FANOUT) as pool:
                    manifests = list(
                        pool.map(self._fetch_global(), (e.artifact_id for e in entries))
                    )
            return [m for m in manifests if m is not None]
        except Exception:
            # Global index unavailable: fall back to the honest scan
            # rather than fail the listing.
            return self._list_scan()

    def list_page(
        self,
        *,
        agent_id: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ArtifactPage:
        if agent_id is not None:
            # One index doc + ≤limit point gets: O(page), independent of
            # the Dict's total artifact count.
            self._ensure_index()
            entries = _index_loads(self._get(self._idx_key(agent_id)))
            return _page_from_entries_par(
                entries,
                cursor=cursor,
                limit=limit,
                fetch=self._fetch_for_agent(agent_id),
                drop=lambda aid: self._index_prune(agent_id, aid),
            )
        try:
            # Chunked global index: O(#chunks + page) reads, independent of
            # the Dict's total artifact count.
            self._ensure_global_index()
            ver = self._get(self._IDX_GLOBAL_VER)
        except Exception:
            ver = _VER_UNREAD
        cache_key = (cursor, limit)
        cached = self._page_cache.get(cache_key)
        if cached is not None:
            age = time.monotonic() - cached[1]
            if ver is _VER_UNREAD:
                # Token unreadable: serve the last good page while fresh
                # and revalidate in the background.
                if age < _LIST_CACHE_STALE_S:
                    self._kick_page_refresh(cache_key)
                    return cached[2]
            elif cached[0] == ver:
                if age < _LIST_CACHE_TTL_S:
                    return cached[2]
                # Past TTL with no confirmed write: serve stale, refresh
                # in the background — callers never block on a page
                # fanout for a cache that merely aged out.
                self._kick_page_refresh(cache_key)
                return cached[2]
            # else: a confirmed write moved ``ver`` — refetch below.
        try:
            entries = self._global_entries()
            page = _page_from_entries_par(
                entries,
                cursor=cursor,
                limit=limit,
                fetch=self._fetch_global(),
                drop=self._global_index_prune,
            )
        except Exception:
            if cached is not None and time.monotonic() - cached[1] < _LIST_CACHE_STALE_S:
                self._kick_page_refresh(cache_key)
                return cached[2]
            return page_manifests(self._list_scan(), cursor=cursor, limit=limit)
        self._page_cache_store(cache_key, None if ver is _VER_UNREAD else ver, page)
        return page

    def delete(self, artifact_id: str) -> None:
        agent_hint: str | None = None
        try:
            agent_hint = self.manifest(artifact_id).producer_agent_id
        except ArtifactError:
            agent_hint = None
        members = self._get(f"{artifact_id}/members") or []
        for name in members:
            self._pop(self._mkey(artifact_id, name))
        self._pop(f"{artifact_id}/members")
        self._pop(f"{artifact_id}/manifest")
        with self._lock:
            self._index_remove_row(artifact_id, agent_hint)
            try:
                self._global_index_remove_locked(artifact_id)
                self._global_ver_bump()
            except Exception:
                self._global_index_broken()

    # ----------------------------------------------------- global index

    def _fetch_global(self) -> Callable[[str], ArtifactManifest | None]:
        def fetch(artifact_id: str) -> ArtifactManifest | None:
            try:
                return self.manifest(artifact_id)
            except ArtifactError:
                return None

        return fetch

    def _global_index_prune(self, artifact_id: str) -> None:
        with self._lock:
            try:
                self._global_index_remove_locked(artifact_id)
            except Exception:
                self._global_index_broken()

    def _global_entries(self) -> list[ArtifactIndexEntry]:
        """All global index rows, sorted; duplicate ids keep the freshest."""
        meta = self._get(self._IDX_GLOBAL_META)
        chunks = int(meta.get("chunks", 0)) if isinstance(meta, dict) else 0
        keys = [f"{self._IDX_GLOBAL_PREFIX}{i:06d}" for i in range(chunks)]
        with observe("modal_dict.get_global_index", store=self._name, chunks=chunks):
            with ThreadPoolExecutor(max_workers=_LIST_FANOUT) as pool:
                raws = list(pool.map(self._d().get, keys))
        entries: list[ArtifactIndexEntry] = []
        for raw in raws:
            entries.extend(_index_loads(raw))
        entries.sort(key=lambda e: e.sort_key)
        deduped: dict[str, ArtifactIndexEntry] = {}
        for entry in entries:
            deduped[entry.artifact_id] = entry
        return sorted(deduped.values(), key=lambda e: e.sort_key)

    def _global_index_add(self, manifest: ArtifactManifest) -> None:
        """RMW the chunked global index: drop any existing row for this id,
        append to the tail chunk, split it at ``_GLOBAL_CHUNK_MAX``."""
        with self._lock:
            self._global_index_remove_locked(manifest.artifact_id)
            meta = self._get(self._IDX_GLOBAL_META)
            chunks = int(meta.get("chunks", 0)) if isinstance(meta, dict) else 0
            tail_key = f"{self._IDX_GLOBAL_PREFIX}{max(chunks - 1, 0):06d}"
            tail = _index_add_entry(_index_loads(self._get(tail_key)), _index_entry(manifest))
            if len(tail) > self._GLOBAL_CHUNK_MAX:
                mid = len(tail) // 2
                self._put(tail_key, _index_dumps(tail[:mid]))
                self._put(f"{self._IDX_GLOBAL_PREFIX}{chunks:06d}", _index_dumps(tail[mid:]))
                chunks += 1
            else:
                self._put(tail_key, _index_dumps(tail))
            self._put(self._IDX_GLOBAL_META, {"chunks": max(chunks, 1)})

    def _global_index_remove_locked(self, artifact_id: str) -> None:
        """Drop ``artifact_id`` from whichever chunk holds it; caller holds
        ``_lock``. Reads all chunk docs — bounded by ``ceil(N/2000)``."""
        meta = self._get(self._IDX_GLOBAL_META)
        chunks = int(meta.get("chunks", 0)) if isinstance(meta, dict) else 0
        if chunks < 1:
            return
        keys = [f"{self._IDX_GLOBAL_PREFIX}{i:06d}" for i in range(chunks)]
        with ThreadPoolExecutor(max_workers=_LIST_FANOUT) as pool:
            raws = list(pool.map(self._d().get, keys))
        for key, raw in zip(keys, raws):
            rows = _index_loads(raw)
            kept = [e for e in rows if e.artifact_id != artifact_id]
            if len(kept) != len(rows):
                self._put(key, _index_dumps(kept))

    def _global_index_broken(self) -> None:
        """Global-index maintenance failed: drop the marker so the next
        listing rebuilds and converges instead of serving drifted rows.
        Cached pages are invalidated too — the version token is dropped
        so no reader can trust a cached page against a drifted index."""
        self._global_ready = False
        self._page_cache.clear()
        try:
            self._pop(self._IDX_GLOBAL_BUILT)
            self._pop(self._IDX_GLOBAL_VER)
        except Exception:
            pass

    def _global_ver_bump(self) -> Any:
        """Advance the global listing-version token after a mutation so
        cached pages in every container refetch. Returns the minted
        token. A failed bump leaves the old token in place — remote
        readers keep serving their (now stale) page for at most
        ``_LIST_CACHE_TTL_S`` before the background revalidation repairs
        it — rather than forcing the expensive global rebuild."""
        try:
            token = uuid.uuid4().hex
            self._put(self._IDX_GLOBAL_VER, token)
        except Exception:
            token = None
        self._page_cache.clear()
        return token

    def _page_cache_store(self, key: tuple[Any, Any], ver: Any, page: ArtifactPage) -> None:
        if len(self._page_cache) >= _PAGE_CACHE_MAX:
            self._page_cache.clear()
        self._page_cache[key] = (ver, time.monotonic(), page)

    def _kick_page_refresh(self, key: tuple[Any, Any]) -> None:
        """Repopulate a cached page off the request path; deduped by
        ``_refresh_lock`` so stacked stale reads share one refetch."""
        if not self._refresh_lock.acquire(blocking=False):
            return
        self._refresh_thread = threading.Thread(
            target=self._refresh_global_page,
            args=(key,),
            name=f"{self._name}-page-refresh",
            daemon=True,
        )
        self._refresh_thread.start()

    def _refresh_global_page(self, key: tuple[Any, Any]) -> None:
        cursor, limit = key
        try:
            self._ensure_global_index()
            ver = self._get(self._IDX_GLOBAL_VER)
            entries = self._global_entries()
            page = _page_from_entries_par(
                entries,
                cursor=cursor,
                limit=limit,
                fetch=self._fetch_global(),
                drop=self._global_index_prune,
            )
            self._page_cache_store(key, ver, page)
        except Exception:
            pass
        finally:
            self._refresh_lock.release()

    def _ensure_global_index(self) -> None:
        """Lazily build the global index on the first unfiltered query —
        the safe migration for Dicts that predate it. ``_build_lock``
        double-checks so concurrent first queries share one rebuild."""
        if self._global_ready:
            return
        with self._build_lock:
            if self._global_ready:
                return
            if self._get(self._IDX_GLOBAL_BUILT) is None:
                self.rebuild_global_index()
            elif self._get(self._IDX_GLOBAL_VER) is None:
                # Index built by a pre-cache deploy: mint the token once
                # so the page cache can key on it.
                self._global_ver_bump()
            # Re-verify both keys rather than trusting the maintenance
            # path — a failed bump leaves the token missing, so the flag
            # stays unset and the next call retries the mint.
            self._global_ready = (
                self._get(self._IDX_GLOBAL_BUILT) is not None
                and self._get(self._IDX_GLOBAL_VER) is not None
            )

    def rebuild_global_index(self) -> int:
        """Re-enumerate keys once, fetch every manifest, and rewrite the
        chunked global index. Idempotent; converges drift (drops stale
        chunk docs). Returns the number of rows written."""
        with observe("modal_dict.keys", store=self._name):
            all_keys = [k for k in self._d().keys() if isinstance(k, str)]
        artifact_ids = [
            key[: -len("/manifest")]
            for key in all_keys
            if key.endswith("/manifest") and "/" not in key[: -len("/manifest")]
        ]
        with observe("modal_dict.get_manifests", store=self._name, manifests=len(artifact_ids)):
            with ThreadPoolExecutor(max_workers=_LIST_FANOUT) as pool:
                raws = list(pool.map(lambda aid: self._d().get(f"{aid}/manifest"), artifact_ids))
        entries: list[ArtifactIndexEntry] = []
        for artifact_id, raw in zip(artifact_ids, raws):
            if raw is None:
                continue
            try:
                manifest = manifest_loads(raw)
            except ArtifactError:
                continue
            if manifest.artifact_id != artifact_id:
                continue
            entries.append(_index_entry(manifest))
        entries.sort(key=lambda e: e.sort_key)
        with self._lock:
            n_chunks = (len(entries) + self._GLOBAL_CHUNK_MAX - 1) // self._GLOBAL_CHUNK_MAX
            keep = set()
            for i in range(n_chunks):
                key = f"{self._IDX_GLOBAL_PREFIX}{i:06d}"
                keep.add(key)
                lo = i * self._GLOBAL_CHUNK_MAX
                self._put(key, _index_dumps(entries[lo : lo + self._GLOBAL_CHUNK_MAX]))
            for key in all_keys:
                if key.startswith(self._IDX_GLOBAL_PREFIX) and key not in keep:
                    self._pop(key)
            self._put(self._IDX_GLOBAL_META, {"chunks": n_chunks})
            self._put(self._IDX_GLOBAL_VER, uuid.uuid4().hex)
            self._put(self._IDX_GLOBAL_BUILT, b"1")
            self._page_cache.clear()
        self._global_ready = True
        return len(entries)


def _to_bytes(value: bytes | str) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else bytes(value)


def _normalize_tests(
    tests: Sequence[TestResult | tuple[str, int] | Mapping[str, Any]],
) -> list[TestResult]:
    out: list[TestResult] = []
    for item in tests:
        if isinstance(item, TestResult):
            out.append(item)
        elif isinstance(item, Mapping):
            out.append(TestResult(str(item["command"]), int(item["exit_code"])))
        else:
            command, code = item
            out.append(TestResult(str(command), int(code)))
    return out


def build_artifact(
    workspace: Path | str,
    *,
    store: ArtifactStore,
    agent_id: str,
    run_id: str | None = None,
    base_sha: str = "",
    head_sha: str = "",
    repo: str = "",
    tests: Sequence[TestResult | tuple[str, int] | Mapping[str, Any]] = (),
    include: Sequence[str] | None = None,
    payloads: Mapping[str, bytes | str] | None = None,
    forbidden_values: Sequence[bytes | str] = (),
    artifact_id: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ArtifactManifest:
    """Collect ``workspace`` into a durable ``patch`` package in ``store``.

    ``include`` narrows the allowlist (default: everything not denied).
    ``payloads`` attaches caller-produced members such as ``patch.diff`` or
    test logs; their names must not collide with ``manifest.json`` or the
    reserved ``files/`` prefix. ``forbidden_values`` fails the whole build
    when a known secret appears inside an allowed file or payload —
    fail-closed, never redact-and-ship.
    """
    forbidden = tuple(_to_bytes(v) for v in forbidden_values)
    policy = WorkspacePolicy(
        include=tuple(include) if include is not None else ("*",),
        forbidden_values=forbidden,
    )
    files, members, warnings = collect_workspace(workspace, policy)
    payload_members: dict[str, bytes] = {}
    payload_digests: dict[str, str] = {}
    for name, raw in (payloads or {}).items():
        err = _member_name_error(name)
        if err is not None or name == MANIFEST_MEMBER or name.startswith(FILES_PREFIX):
            raise ArtifactError(f"payload name not allowed: {name!r}")
        data = _to_bytes(raw)
        if any(value and value in data for value in forbidden):
            raise ArtifactSecretError([name])
        payload_members[name] = data
        payload_digests[name] = sha256_hex(data)
    now = (clock or (lambda: datetime.now(UTC)))().isoformat()
    manifest = ArtifactManifest(
        artifact_id=artifact_id or f"art-{uuid.uuid4().hex[:16]}",
        base_sha=base_sha,
        head_sha=head_sha,
        repo=repo,
        created_at=now,
        producer_agent_id=agent_id,
        producer_run_id=run_id,
        files=files,
        tests=_normalize_tests(tests),
        payloads=payload_digests,
        warnings=warnings,
    )
    members.update(payload_members)
    return store.put(manifest, members)


__all__ = [
    "ARTIFACT_FORMAT_PATCH",
    "ARTIFACTS_DICT_NAME",
    "FILES_PREFIX",
    "MANIFEST_MEMBER",
    "PATCH_MEMBER",
    "SCHEMA_VERSION",
    "ArtifactCorruptError",
    "ArtifactError",
    "ArtifactFile",
    "ArtifactIndexEntry",
    "ArtifactManifest",
    "ArtifactNotFoundError",
    "ArtifactPackage",
    "ArtifactPage",
    "ArtifactSecretError",
    "ArtifactStore",
    "FileArtifactStore",
    "InMemoryArtifactStore",
    "ModalDictArtifactStore",
    "TestResult",
    "WorkspacePolicy",
    "build_artifact",
    "collect_workspace",
    "manifest_dumps",
    "manifest_from_dict",
    "manifest_loads",
    "manifest_to_dict",
    "page_manifests",
    "sha256_hex",
]
