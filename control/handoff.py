"""Cross-agent handoff prepare (SOR-83/B2, SOR-90).

A downstream agent starts from an ``artifact_id`` (a B1 artifact package:
manifest + patch/bundle payload) or an exact ``head_sha`` in the same repo.
Both paths validate BEFORE anything is applied or checked out:

* the manifest's ``base_sha`` must equal the workspace's current recorded
  head — a mismatch is an explicit ``base_sha_mismatch`` failure, never a
  silent apply onto the wrong version;
* the payload's sha256 must equal ``manifest.payload_sha256`` —
  ``checksum_mismatch``;
* an exact ``head_sha`` must resolve to a commit that descends from the
  declared base — otherwise ``base_sha_mismatch``.

Artifact persistence is owned by SOR-83/B1; this module defines the narrow
``ArtifactStore`` / ``ArtifactManifest`` contract it implements, plus an
in-memory reference implementation for tests and local wiring.

``kind="bundle"`` reproduces the producer's exact commit (the recorded
``head_sha`` equals ``manifest.head_sha``), so it is the path for chained
handoffs and sha-pinned reviews. ``kind="patch"`` applies a unified diff and
commits it locally — content is verified via ``manifest.files`` checksums,
while the resulting commit is a new object in the downstream repo.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

from control.backend import SandboxHandle
from control.workspace import (
    ARTIFACT_INVALID,
    ARTIFACT_NOT_FOUND,
    BASE_SHA_MISMATCH,
    CHECKOUT_FAILED,
    CHECKSUM_MISMATCH,
    DEFAULT_WORKDIR,
    HEAD_SHA_MISMATCH,
    WORKSPACE_INVALID,
    WorkspaceError,
    WorkspaceRecord,
    WorkspaceService,
    WorkspaceSpec,
    git_checkout,
    git_head,
    git_is_ancestor,
    git_rev_parse,
    is_commit_sha,
    is_safe_relpath,
    is_sha256,
    run_git,
    sha256_file,
    write_payload,
)

ARTIFACT_KIND_PATCH = "patch"
ARTIFACT_KIND_BUNDLE = "bundle"
ARTIFACT_KINDS = (ARTIFACT_KIND_PATCH, ARTIFACT_KIND_BUNDLE)

# Sandbox-root-relative staging area for payloads; kept OUTSIDE the workdir
# so ``git add -A`` can never sweep a payload into the handoff commit.
_STAGING_DIR = ".sbx-handoff"


@dataclass(frozen=True)
class ArtifactManifest:
    """The manifest.json fields handoff validation depends on.

    ``files`` maps repo-relative paths to the sha256 of their post-handoff
    content; ``payload_sha256`` is the sha256 of the raw patch/bundle bytes.
    """

    artifact_id: str
    kind: str
    repo: str
    base_sha: str
    head_sha: str
    payload_sha256: str
    files: dict[str, str] = field(default_factory=dict)
    test_command: str | None = None
    test_exit_code: int | None = None
    producer_agent_id: str | None = None
    producer_run_id: str | None = None
    created_at: str = ""


def manifest_to_dict(manifest: ArtifactManifest) -> dict[str, Any]:
    return asdict(manifest)


def manifest_from_dict(data: Any) -> ArtifactManifest:
    """Strict decode; raises ``ValueError`` on unusable payloads."""
    if not isinstance(data, dict):
        raise ValueError("artifact manifest is not a dict")
    artifact_id = _required_str(data, "artifact_id")
    kind = _required_str(data, "kind")
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"artifact manifest kind must be one of {ARTIFACT_KINDS}: {kind!r}")
    repo = _required_str(data, "repo")
    base_sha = _required_str(data, "base_sha")
    head_sha = _required_str(data, "head_sha")
    if not is_commit_sha(base_sha):
        raise ValueError(f"manifest base_sha must be a 40-hex commit sha: {base_sha!r}")
    if not is_commit_sha(head_sha):
        raise ValueError(f"manifest head_sha must be a 40-hex commit sha: {head_sha!r}")
    payload_sha256 = _required_str(data, "payload_sha256")
    if not is_sha256(payload_sha256):
        raise ValueError(f"manifest payload_sha256 must be a sha256 hex: {payload_sha256!r}")
    files_raw = data.get("files")
    if files_raw is None:
        files_raw = {}
    if not isinstance(files_raw, dict):
        raise ValueError("manifest files must be a dict of relpath -> sha256")
    files: dict[str, str] = {}
    for relpath, sha in files_raw.items():
        if not is_safe_relpath(relpath):
            raise ValueError(f"manifest file path is not repo-relative: {relpath!r}")
        if not is_sha256(sha):
            raise ValueError(f"manifest file checksum must be sha256 hex: {relpath!r}")
        files[relpath] = sha
    test_exit_code = data.get("test_exit_code")
    if test_exit_code is not None and (
        isinstance(test_exit_code, bool) or not isinstance(test_exit_code, int)
    ):
        raise ValueError("manifest test_exit_code must be an int")
    optional: dict[str, str | None] = {}
    for key in ("test_command", "producer_agent_id", "producer_run_id"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"manifest {key} must be a string")
        optional[key] = value
    created_at = data.get("created_at")
    if created_at is not None and not isinstance(created_at, str):
        raise ValueError("manifest created_at must be a string")
    return ArtifactManifest(
        artifact_id=artifact_id,
        kind=kind,
        repo=repo,
        base_sha=base_sha,
        head_sha=head_sha,
        payload_sha256=payload_sha256,
        files=files,
        test_exit_code=test_exit_code,
        created_at=created_at or "",
        **optional,
    )


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"artifact manifest missing {key}")
    return value


@runtime_checkable
class ArtifactStore(Protocol):
    """Narrow read surface B2 consumes; B1 owns durable persistence.

    ``get_manifest`` returns the raw manifest.json dict (B2 owns decode +
    validation so malformed manifests fail identically for every store).
    ``read_payload`` returns the patch/bundle bytes. Both return None when
    the artifact does not exist; a present manifest with a missing payload
    is ``artifact_invalid``.
    """

    def get_manifest(self, artifact_id: str) -> dict[str, Any] | None:
        """Raw manifest dict, or None when the artifact does not exist."""

    def read_payload(self, artifact_id: str) -> bytes | None:
        """Patch/bundle payload bytes, or None when absent."""


class InMemoryArtifactStore:
    """Reference ``ArtifactStore`` for tests and local wiring."""

    def __init__(self) -> None:
        self._manifests: dict[str, dict[str, Any]] = {}
        self._payloads: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, manifest: ArtifactManifest, payload: bytes) -> str:
        with self._lock:
            self._manifests[manifest.artifact_id] = manifest_to_dict(manifest)
            self._payloads[manifest.artifact_id] = bytes(payload)
        return manifest.artifact_id

    def put_raw(
        self, artifact_id: str, manifest: dict[str, Any], payload: bytes | None = None
    ) -> None:
        """Store verbatim (tests inject malformed/absent payloads this way)."""
        with self._lock:
            self._manifests[artifact_id] = dict(manifest)
            if payload is not None:
                self._payloads[artifact_id] = bytes(payload)

    def get_manifest(self, artifact_id: str) -> dict[str, Any] | None:
        with self._lock:
            raw = self._manifests.get(artifact_id)
        return dict(raw) if raw is not None else None

    def read_payload(self, artifact_id: str) -> bytes | None:
        with self._lock:
            return self._payloads.get(artifact_id)


class HandoffService:
    """Prepare a downstream workspace from an artifact_id or exact head_sha.

    Delegates clone/base verification to ``WorkspaceService``; every failure
    is an explicit ``WorkspaceError`` — nothing applies or checks out on an
    unverified base.
    """

    def __init__(self, workspaces: WorkspaceService, artifacts: ArtifactStore) -> None:
        self._workspaces = workspaces
        self._artifacts = artifacts

    def prepare_from_head(
        self,
        handle: SandboxHandle,
        agent_id: str,
        head_sha: str,
        *,
        spec: WorkspaceSpec | None = None,
        workdir: str = DEFAULT_WORKDIR,
    ) -> WorkspaceRecord:
        """Check out an exact commit the downstream task was handed.

        The commit must exist in the workspace repo and descend from the
        declared ``base_sha`` — a head that forked off a different base is an
        explicit ``base_sha_mismatch``.
        """
        if not is_commit_sha(head_sha):
            raise WorkspaceError(
                WORKSPACE_INVALID, f"head_sha must be a 40-hex commit sha: {head_sha!r}"
            )
        record = self._ensure_base(handle, agent_id, spec, workdir)
        backend = self._workspaces.backend
        self._assert_at_recorded_head(handle, record)
        if git_rev_parse(backend, handle, record.workdir, head_sha) != head_sha:
            raise WorkspaceError(
                CHECKOUT_FAILED, f"head_sha {head_sha} is not a commit in {record.repo!r}"
            )
        if not git_is_ancestor(backend, handle, record.workdir, record.base_sha, head_sha):
            raise WorkspaceError(
                BASE_SHA_MISMATCH,
                f"head_sha {head_sha} does not descend from declared base {record.base_sha}",
            )
        git_checkout(backend, handle, record.workdir, head_sha)
        actual = git_head(backend, handle, record.workdir)
        if actual != head_sha:
            raise WorkspaceError(
                HEAD_SHA_MISMATCH, f"checkout drifted: HEAD is {actual}, expected {head_sha}"
            )
        record.checkout_sha = head_sha
        record.head_sha = head_sha
        return self._workspaces.save(record)

    def prepare_from_artifact(
        self,
        handle: SandboxHandle,
        agent_id: str,
        artifact_id: str,
        *,
        spec: WorkspaceSpec | None = None,
        workdir: str = DEFAULT_WORKDIR,
    ) -> WorkspaceRecord:
        """Apply a B1 artifact package onto the workspace.

        Validation order is deliberate: manifest decode → repo identity →
        payload checksum — all before the workdir is touched. Then the
        workdir must sit exactly on the recorded head, and the artifact's
        declared ``base_sha`` must equal that head.
        """
        manifest = self._manifest(artifact_id)
        if spec is not None and manifest.repo != spec.repo:
            raise WorkspaceError(
                ARTIFACT_INVALID,
                f"artifact repo {manifest.repo!r} does not match declared repo {spec.repo!r}",
            )
        payload = self._artifacts.read_payload(artifact_id)
        if payload is None:
            raise WorkspaceError(ARTIFACT_INVALID, f"artifact {artifact_id} has no payload")
        digest = hashlib.sha256(payload).hexdigest()
        if digest != manifest.payload_sha256:
            raise WorkspaceError(
                CHECKSUM_MISMATCH,
                f"artifact {artifact_id} payload sha256 {digest} "
                f"!= manifest {manifest.payload_sha256}",
            )
        record = self._ensure_base(handle, agent_id, spec, workdir)
        if manifest.repo != record.repo:
            raise WorkspaceError(
                ARTIFACT_INVALID,
                f"artifact repo {manifest.repo!r} does not match workspace repo {record.repo!r}",
            )
        current = self._assert_at_recorded_head(handle, record)
        if manifest.base_sha != current:
            raise WorkspaceError(
                BASE_SHA_MISMATCH,
                f"artifact {artifact_id} declares base {manifest.base_sha} "
                f"but workspace head is {current}",
            )
        if manifest.kind == ARTIFACT_KIND_BUNDLE:
            actual = self._fetch_bundle(handle, record.workdir, manifest, payload)
        else:
            actual = self._apply_patch(handle, record.workdir, manifest, payload)
        self._verify_files(handle, record.workdir, manifest.files)
        record.checkout_sha = actual
        record.head_sha = actual
        return self._workspaces.save(record)

    def _manifest(self, artifact_id: str) -> ArtifactManifest:
        raw = self._artifacts.get_manifest(artifact_id)
        if raw is None:
            raise WorkspaceError(ARTIFACT_NOT_FOUND, f"unknown artifact {artifact_id!r}")
        try:
            return manifest_from_dict(raw)
        except (ValueError, TypeError) as exc:
            raise WorkspaceError(
                ARTIFACT_INVALID, f"artifact {artifact_id} manifest is malformed: {exc}"
            ) from exc

    def _ensure_base(
        self,
        handle: SandboxHandle,
        agent_id: str,
        spec: WorkspaceSpec | None,
        workdir: str,
    ) -> WorkspaceRecord:
        """The workspace the handoff applies onto: existing prepared record,
        or a fresh ``prepare`` from the supplied spec."""
        record = self._workspaces.get(agent_id)
        if record is None:
            if spec is None:
                raise WorkspaceError(
                    WORKSPACE_INVALID,
                    f"agent {agent_id} has no workspace; pass a WorkspaceSpec to declare one",
                )
            return self._workspaces.prepare(handle, agent_id, spec, workdir=workdir)
        if spec is not None and spec != record.spec:
            raise WorkspaceError(
                WORKSPACE_INVALID,
                f"spec conflicts with the workspace declared for agent {agent_id}",
            )
        if not record.prepared:
            raise WorkspaceError(
                WORKSPACE_INVALID,
                f"workspace for agent {agent_id} is declared but not prepared",
            )
        return record

    def _assert_at_recorded_head(self, handle: SandboxHandle, record: WorkspaceRecord) -> str:
        """Workdir HEAD must equal the recorded head — unrecorded drift means
        the base the artifact was built against may no longer be present."""
        current = git_head(self._workspaces.backend, handle, record.workdir)
        if current != record.head_sha:
            raise WorkspaceError(
                BASE_SHA_MISMATCH,
                f"workdir {record.workdir} HEAD {current} has drifted "
                f"from recorded head {record.head_sha}",
            )
        return current

    def _apply_patch(
        self,
        handle: SandboxHandle,
        workdir: str,
        manifest: ArtifactManifest,
        payload: bytes,
    ) -> str:
        """``git apply`` + commit. Returns the actual new head sha."""
        backend = self._workspaces.backend
        staging = f"{_STAGING_DIR}/{manifest.artifact_id}.patch"
        write_payload(backend, handle, staging, payload)
        target = str(handle.root / staging)
        res = run_git(backend, handle, ["apply", "--check", target], cwd=workdir)
        if res.code != 0:
            raise WorkspaceError(
                ARTIFACT_INVALID,
                f"artifact {manifest.artifact_id} patch does not apply on the workspace base",
            )
        res = run_git(backend, handle, ["apply", target], cwd=workdir)
        if res.code != 0:
            raise WorkspaceError(
                ARTIFACT_INVALID, f"artifact {manifest.artifact_id} failed to apply"
            )
        dirty = run_git(backend, handle, ["status", "--porcelain"], cwd=workdir)
        if dirty.code != 0:
            raise WorkspaceError(CHECKOUT_FAILED, "git status failed after patch apply")
        if dirty.lines:
            res = run_git(backend, handle, ["add", "-A"], cwd=workdir)
            if res.code == 0:
                res = run_git(
                    backend,
                    handle,
                    [
                        "-c",
                        "user.name=sbx-handoff",
                        "-c",
                        "user.email=sbx-handoff@localhost",
                        "commit",
                        "-qm",
                        f"handoff {manifest.artifact_id}",
                    ],
                    cwd=workdir,
                )
            if res.code != 0:
                raise WorkspaceError(CHECKOUT_FAILED, "failed to commit applied artifact payload")
        actual = git_head(backend, handle, workdir)
        if actual is None:
            raise WorkspaceError(CHECKOUT_FAILED, f"no HEAD in workdir {workdir} after apply")
        return actual

    def _fetch_bundle(
        self,
        handle: SandboxHandle,
        workdir: str,
        manifest: ArtifactManifest,
        payload: bytes,
    ) -> str:
        """Fetch a git bundle and check out ``manifest.head_sha`` exactly."""
        backend = self._workspaces.backend
        staging = f"{_STAGING_DIR}/{manifest.artifact_id}.bundle"
        write_payload(backend, handle, staging, payload)
        bundle = str(handle.root / staging)
        heads = run_git(backend, handle, ["bundle", "list-heads", bundle], cwd=workdir)
        if heads.code != 0:
            raise WorkspaceError(
                ARTIFACT_INVALID, f"artifact {manifest.artifact_id} payload is not a git bundle"
            )
        ref = None
        for line in heads.lines:
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0] == manifest.head_sha:
                ref = parts[1].strip()
                break
        if ref is None:
            raise WorkspaceError(
                HEAD_SHA_MISMATCH,
                f"bundle for artifact {manifest.artifact_id} does not contain "
                f"head {manifest.head_sha}",
            )
        res = run_git(backend, handle, ["fetch", bundle, ref], cwd=workdir)
        if res.code != 0:
            raise WorkspaceError(
                CHECKOUT_FAILED,
                f"bundle fetch failed for artifact {manifest.artifact_id} "
                "(prerequisite commits missing?)",
            )
        git_checkout(backend, handle, workdir, manifest.head_sha)
        actual = git_head(backend, handle, workdir)
        if actual != manifest.head_sha:
            raise WorkspaceError(
                HEAD_SHA_MISMATCH,
                f"checkout drifted: HEAD is {actual}, expected {manifest.head_sha}",
            )
        return actual

    def _verify_files(self, handle: SandboxHandle, workdir: str, files: dict[str, str]) -> None:
        """Post-apply content verification against manifest checksums."""
        backend = self._workspaces.backend
        for relpath, expected in files.items():
            actual = sha256_file(backend, handle, f"{workdir}/{relpath}")
            if actual != expected:
                raise WorkspaceError(
                    CHECKSUM_MISMATCH,
                    f"{relpath}: sha256 {actual or 'missing'} != manifest {expected}",
                )


__all__ = [
    "ARTIFACT_KINDS",
    "ARTIFACT_KIND_BUNDLE",
    "ARTIFACT_KIND_PATCH",
    "ArtifactManifest",
    "ArtifactStore",
    "HandoffService",
    "InMemoryArtifactStore",
    "manifest_from_dict",
    "manifest_to_dict",
]
