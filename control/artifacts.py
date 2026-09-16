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

This module is the B1 core only: no workspace declaration, no API routes.
Integration lanes wire a collector over ``SandboxBackend.exec`` for remote
sandboxes and expose ``read`` as the download seam.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from control.config import ARTIFACTS_DICT_NAME

ARTIFACT_FORMAT_PATCH = "patch"
MANIFEST_MEMBER = "manifest.json"
PATCH_MEMBER = "patch.diff"  # conventional caller-supplied unified diff member
FILES_PREFIX = "files/"
SCHEMA_VERSION = 1

_ARTIFACT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

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


@runtime_checkable
class ArtifactStore(Protocol):
    """Persistence for artifact packages, keyed by artifact_id.

    Implementations must verify member integrity against the manifest on
    ``put`` and ``open``/``read`` — a store that persists mismatched bytes
    silently is a corruption amplifier.
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
        self._lock = threading.Lock()

    def put(self, manifest: ArtifactManifest, members: Mapping[str, bytes]) -> ArtifactManifest:
        _validate_artifact_id(manifest.artifact_id)
        _verify_members(manifest, members)
        with self._lock:
            self._items[manifest.artifact_id] = (
                manifest_dumps(manifest),
                dict(members),
            )
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

    def list(self, *, agent_id: str | None = None) -> list[ArtifactManifest]:
        with self._lock:
            keys = list(self._items)
        out: list[ArtifactManifest] = []
        for key in keys:
            try:
                manifest = self.manifest(key)
            except ArtifactError:
                continue
            if agent_id is None or manifest.producer_agent_id == agent_id:
                out.append(manifest)
        return sorted(out, key=lambda m: (m.created_at, m.artifact_id))

    def delete(self, artifact_id: str) -> None:
        with self._lock:
            self._items.pop(artifact_id, None)


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
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root

    def _dir(self, artifact_id: str) -> Path:
        return self._root / _validate_artifact_id(artifact_id)

    def put(self, manifest: ArtifactManifest, members: Mapping[str, bytes]) -> ArtifactManifest:
        _validate_artifact_id(manifest.artifact_id)
        _verify_members(manifest, members)
        target = self._dir(manifest.artifact_id)
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

    def list(self, *, agent_id: str | None = None) -> list[ArtifactManifest]:
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
            if agent_id is None or manifest.producer_agent_id == agent_id:
                out.append(manifest)
        return sorted(out, key=lambda m: (m.created_at, m.artifact_id))

    def delete(self, artifact_id: str) -> None:
        _validate_artifact_id(artifact_id)
        with self._lock:
            shutil.rmtree(self._dir(artifact_id), ignore_errors=True)


class ModalDictArtifactStore:
    """Production store backed by ``modal.Dict``. Lazy-imports modal.

    Keys: ``<id>/manifest`` -> canonical manifest bytes, ``<id>/members``
    -> member name list, ``<id>/member/<name>`` -> bytes.
    """

    def __init__(self, name: str = ARTIFACTS_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    @staticmethod
    def _mkey(artifact_id: str, member: str) -> str:
        return f"{artifact_id}/member/{member}"

    def put(self, manifest: ArtifactManifest, members: Mapping[str, bytes]) -> ArtifactManifest:
        _validate_artifact_id(manifest.artifact_id)
        _verify_members(manifest, members)
        aid = manifest.artifact_id
        for name, data in members.items():
            self._d().put(self._mkey(aid, name), data)
        self._d().put(f"{aid}/members", sorted(members))
        self._d().put(f"{aid}/manifest", manifest_dumps(manifest))
        return manifest

    def manifest(self, artifact_id: str) -> ArtifactManifest:
        raw = self._d().get(f"{artifact_id}/manifest")
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
        members: dict[str, bytes] = {}
        for name in _declared_members(manifest):
            data = self._d().get(self._mkey(artifact_id, name))
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
        data = self._d().get(self._mkey(artifact_id, member))
        if data is None:
            raise ArtifactCorruptError(f"member {member!r} missing from store")
        if size is not None and len(data) != size:
            raise ArtifactCorruptError(f"member {member!r} size mismatch")
        if sha256_hex(data) != digest:
            raise ArtifactCorruptError(f"member {member!r} checksum mismatch")
        return data

    def list(self, *, agent_id: str | None = None) -> list[ArtifactManifest]:
        out: list[ArtifactManifest] = []
        items: Iterator[tuple[Any, Any]] = self._d().items()
        for key, _raw in items:
            if not isinstance(key, str) or not key.endswith("/manifest"):
                continue
            try:
                manifest = self.manifest(key[: -len("/manifest")])
            except ArtifactError:
                continue
            if agent_id is None or manifest.producer_agent_id == agent_id:
                out.append(manifest)
        return sorted(out, key=lambda m: (m.created_at, m.artifact_id))

    def delete(self, artifact_id: str) -> None:
        d = self._d()
        members = d.get(f"{artifact_id}/members") or []
        for name in members:
            try:
                d.pop(self._mkey(artifact_id, name))
            except KeyError:
                pass
        for key in (f"{artifact_id}/members", f"{artifact_id}/manifest"):
            try:
                d.pop(key)
            except KeyError:
                pass


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
    "ArtifactManifest",
    "ArtifactNotFoundError",
    "ArtifactPackage",
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
    "sha256_hex",
]
