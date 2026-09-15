"""Handoff prepare: artifact_id / exact head_sha with base+checksum gates."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
from control.backend import LocalProcessBackend, SandboxHandle, SandboxSpec
from control.handoff import (
    ARTIFACT_KIND_BUNDLE,
    ARTIFACT_KIND_PATCH,
    ArtifactManifest,
    HandoffService,
    InMemoryArtifactStore,
    manifest_from_dict,
    manifest_to_dict,
)
from control.workspace import (
    ARTIFACT_INVALID,
    ARTIFACT_NOT_FOUND,
    BASE_SHA_MISMATCH,
    CHECKOUT_FAILED,
    CHECKSUM_MISMATCH,
    HEAD_SHA_MISMATCH,
    WORKSPACE_INVALID,
    InMemoryWorkspaceStore,
    WorkspaceError,
    WorkspaceService,
    WorkspaceSpec,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "sbx-test",
    "GIT_AUTHOR_EMAIL": "sbx-test@localhost",
    "GIT_COMMITTER_NAME": "sbx-test",
    "GIT_COMMITTER_EMAIL": "sbx-test@localhost",
}

SHA_0 = "0" * 40


def host_git(cwd: Path, *args: str, raw: bool = False) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout if raw else res.stdout.strip()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_repo(root: Path, name: str = "origin") -> tuple[Path, str]:
    repo = root / name
    repo.mkdir()
    host_git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    host_git(repo, "add", "-A")
    host_git(repo, "commit", "-qm", "A")
    return repo, host_git(repo, "rev-parse", "HEAD")


def producer_commit(
    origin: Path, root: Path, filename: str = "b.txt", content: str = "two\n"
) -> str:
    """Clone ``origin``, commit one file on top of main, push the head back as
    ``refs/heads/work-<filename>`` so downstream clones can fetch it; return
    the head sha."""
    prod = root / f"prod-{filename}"
    host_git(root, "clone", "-q", str(origin), prod.name)
    (prod / filename).write_text(content, encoding="utf-8")
    host_git(prod, "add", "-A")
    host_git(prod, "commit", "-qm", f"add {filename}")
    head = host_git(prod, "rev-parse", "HEAD")
    host_git(prod, "push", "-q", str(origin), f"HEAD:refs/heads/work-{filename}")
    return head


def make_patch_artifact(
    origin: Path, root: Path, artifact_id: str, filename: str = "b.txt", content: str = "two\n"
) -> tuple[ArtifactManifest, bytes]:
    base = host_git(origin, "rev-parse", "main")
    head = producer_commit(origin, root, filename, content)
    prod = root / f"prod-{filename}"
    payload = host_git(prod, "diff", base, head, raw=True).encode()
    files = {name: sha256((prod / name).read_bytes()) for name in ("a.txt", filename)}
    manifest = ArtifactManifest(
        artifact_id=artifact_id,
        kind=ARTIFACT_KIND_PATCH,
        repo=str(origin),
        base_sha=base,
        head_sha=head,
        payload_sha256=sha256(payload),
        files=files,
        test_command="make test",
        test_exit_code=0,
        producer_agent_id="prod-agent",
        producer_run_id="run-1",
        created_at="2026-09-15T00:00:00+00:00",
    )
    return manifest, payload


def make_bundle_artifact(
    origin: Path, root: Path, artifact_id: str, filename: str = "b.txt", content: str = "two\n"
) -> tuple[ArtifactManifest, bytes]:
    base = host_git(origin, "rev-parse", "main")
    head = producer_commit(origin, root, filename, content)
    bundle_path = root / f"{artifact_id}.bundle"
    host_git(root / f"prod-{filename}", "bundle", "create", str(bundle_path), "main")
    payload = bundle_path.read_bytes()
    prod = root / f"prod-{filename}"
    files = {name: sha256((prod / name).read_bytes()) for name in ("a.txt", filename)}
    manifest = ArtifactManifest(
        artifact_id=artifact_id,
        kind=ARTIFACT_KIND_BUNDLE,
        repo=str(origin),
        base_sha=base,
        head_sha=head,
        payload_sha256=sha256(payload),
        files=files,
    )
    return manifest, payload


@pytest.fixture
def backend() -> LocalProcessBackend:
    return LocalProcessBackend()


@pytest.fixture
def handle(backend: LocalProcessBackend) -> SandboxHandle:
    return backend.create(SandboxSpec())


@pytest.fixture
def workspaces(backend: LocalProcessBackend) -> WorkspaceService:
    return WorkspaceService(backend, InMemoryWorkspaceStore())


@pytest.fixture
def artifacts() -> InMemoryArtifactStore:
    return InMemoryArtifactStore()


@pytest.fixture
def handoff(workspaces: WorkspaceService, artifacts: InMemoryArtifactStore) -> HandoffService:
    return HandoffService(workspaces, artifacts)


def spec(repo: Path, base_sha: str) -> WorkspaceSpec:
    return WorkspaceSpec(repo=str(repo), base_ref="main", base_sha=base_sha)


class TestManifestCodec:
    def test_roundtrip(self, tmp_path: Path) -> None:
        manifest, _ = make_patch_artifact(make_repo(tmp_path)[0], tmp_path, "art-1")
        assert manifest_from_dict(manifest_to_dict(manifest)) == manifest

    @pytest.mark.parametrize(
        "patch",
        [
            {"kind": "zip"},
            {"base_sha": "zz"},
            {"head_sha": "zz"},
            {"payload_sha256": "zz"},
            {"files": {"../escape": "a" * 64}},
            {"files": {"ok.txt": "zz"}},
            {"files": ["a.txt"]},
            {"test_exit_code": "0"},
        ],
    )
    def test_rejects_junk(self, tmp_path: Path, patch: dict) -> None:
        manifest, _ = make_patch_artifact(make_repo(tmp_path)[0], tmp_path, "art-1")
        data = manifest_to_dict(manifest) | patch
        with pytest.raises(ValueError):
            manifest_from_dict(data)

    def test_missing_required(self) -> None:
        for key in ("artifact_id", "kind", "repo", "base_sha", "head_sha", "payload_sha256"):
            with pytest.raises(ValueError):
                manifest_from_dict({key: None})


class TestStore:
    def test_roundtrip(self, tmp_path: Path) -> None:
        manifest, payload = make_patch_artifact(make_repo(tmp_path)[0], tmp_path, "art-1")
        store = InMemoryArtifactStore()
        store.put(manifest, payload)
        assert manifest_from_dict(store.get_manifest("art-1")) == manifest
        assert store.read_payload("art-1") == payload
        assert store.get_manifest("nope") is None
        assert store.read_payload("nope") is None


class TestPrepareFromHead:
    def test_checkout_exact_head(
        self, tmp_path: Path, handle: SandboxHandle, handoff: HandoffService
    ) -> None:
        origin, base = make_repo(tmp_path)
        head = producer_commit(origin, tmp_path)
        record = handoff.prepare_from_head(handle, "a1", head, spec=spec(origin, base))
        assert record.checkout_sha == head
        assert record.head_sha == head
        assert host_git(handle.root / "repo", "rev-parse", "HEAD") == head
        assert (handle.root / "repo" / "b.txt").read_text() == "two\n"

    def test_existing_prepared_workspace(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        workspaces: WorkspaceService,
    ) -> None:
        origin, base = make_repo(tmp_path)
        head = producer_commit(origin, tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        record = handoff.prepare_from_head(handle, "a1", head)
        assert record.head_sha == head

    def test_conflicting_spec_rejected(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        workspaces: WorkspaceService,
    ) -> None:
        origin, base = make_repo(tmp_path)
        head = producer_commit(origin, tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        other = WorkspaceSpec(repo=str(origin), base_ref="main", base_sha=SHA_0)
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_head(handle, "a1", head, spec=other)
        assert exc.value.code == WORKSPACE_INVALID

    def test_no_workspace_needs_spec(
        self, tmp_path: Path, handle: SandboxHandle, handoff: HandoffService
    ) -> None:
        origin, _ = make_repo(tmp_path)
        head = producer_commit(origin, tmp_path)
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_head(handle, "a1", head)
        assert exc.value.code == WORKSPACE_INVALID

    def test_bad_sha_format(
        self, tmp_path: Path, handle: SandboxHandle, handoff: HandoffService
    ) -> None:
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_head(handle, "a1", "notasha")
        assert exc.value.code == WORKSPACE_INVALID

    def test_missing_commit(
        self, tmp_path: Path, handle: SandboxHandle, handoff: HandoffService
    ) -> None:
        origin, base = make_repo(tmp_path)
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_head(handle, "a1", "1" * 40, spec=spec(origin, base))
        assert exc.value.code == CHECKOUT_FAILED

    def test_head_not_descending_from_base(
        self, tmp_path: Path, handle: SandboxHandle, handoff: HandoffService
    ) -> None:
        origin, base = make_repo(tmp_path)
        host_git(origin, "checkout", "-q", "--orphan", "other")
        host_git(origin, "commit", "-qm", "D", "--allow-empty")
        other = host_git(origin, "rev-parse", "other")
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_head(handle, "a1", other, spec=spec(origin, base))
        assert exc.value.code == BASE_SHA_MISMATCH


class TestPrepareFromArtifact:
    def test_patch_applies_and_records_head(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        artifacts: InMemoryArtifactStore,
    ) -> None:
        origin, base = make_repo(tmp_path)
        manifest, payload = make_patch_artifact(origin, tmp_path, "art-1")
        artifacts.put(manifest, payload)
        record = handoff.prepare_from_artifact(handle, "a1", "art-1", spec=spec(origin, base))
        assert (handle.root / "repo" / "b.txt").read_text() == "two\n"
        assert (handle.root / "repo" / "a.txt").read_text() == "one\n"
        actual = host_git(handle.root / "repo", "rev-parse", "HEAD")
        assert record.head_sha == record.checkout_sha == actual != base
        assert host_git(handle.root / "repo", "status", "--porcelain") == ""

    def test_patch_onto_existing_workspace(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        workspaces: WorkspaceService,
        artifacts: InMemoryArtifactStore,
    ) -> None:
        origin, base = make_repo(tmp_path)
        manifest, payload = make_patch_artifact(origin, tmp_path, "art-1")
        artifacts.put(manifest, payload)
        workspaces.prepare(handle, "a1", spec(origin, base))
        record = handoff.prepare_from_artifact(handle, "a1", "art-1")
        assert (handle.root / "repo" / "b.txt").is_file()
        assert record.head_sha != base

    def test_bundle_reproduces_exact_head(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        artifacts: InMemoryArtifactStore,
    ) -> None:
        origin, base = make_repo(tmp_path)
        manifest, payload = make_bundle_artifact(origin, tmp_path, "art-1")
        artifacts.put(manifest, payload)
        record = handoff.prepare_from_artifact(handle, "a1", "art-1", spec=spec(origin, base))
        assert record.checkout_sha == record.head_sha == manifest.head_sha != base
        assert (handle.root / "repo" / "b.txt").read_text() == "two\n"

    def test_chained_bundles(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        workspaces: WorkspaceService,
        artifacts: InMemoryArtifactStore,
    ) -> None:
        origin, base = make_repo(tmp_path)
        m1, p1 = make_bundle_artifact(origin, tmp_path, "art-1", "b.txt", "two\n")
        # Producer builds a second artifact on top of art-1's head.
        prod = tmp_path / "prod-b.txt"
        (prod / "c.txt").write_text("three\n", encoding="utf-8")
        host_git(prod, "add", "-A")
        host_git(prod, "commit", "-qm", "add c.txt")
        head2 = host_git(prod, "rev-parse", "HEAD")
        bundle2 = tmp_path / "art-2.bundle"
        host_git(prod, "bundle", "create", str(bundle2), "main")
        payload2 = bundle2.read_bytes()
        m2 = ArtifactManifest(
            artifact_id="art-2",
            kind=ARTIFACT_KIND_BUNDLE,
            repo=str(origin),
            base_sha=m1.head_sha,
            head_sha=head2,
            payload_sha256=sha256(payload2),
            files={"c.txt": sha256(b"three\n")},
        )
        artifacts.put(m1, p1)
        artifacts.put(m2, payload2)
        handoff.prepare_from_artifact(handle, "a1", "art-1", spec=spec(origin, base))
        record = handoff.prepare_from_artifact(handle, "a1", "art-2")
        assert record.head_sha == head2
        assert (handle.root / "repo" / "c.txt").read_text() == "three\n"
        # Review pins the exact version that was handed off.
        reviewed = workspaces.mark_reviewed("a1")
        assert reviewed.reviewed_head_sha == head2

    def test_unknown_artifact(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        workspaces: WorkspaceService,
    ) -> None:
        origin, base = make_repo(tmp_path)
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_artifact(handle, "a1", "nope", spec=spec(origin, base))
        assert exc.value.code == ARTIFACT_NOT_FOUND
        assert workspaces.get("a1") is None

    def test_malformed_manifest(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        artifacts: InMemoryArtifactStore,
    ) -> None:
        origin, base = make_repo(tmp_path)
        manifest, payload = make_patch_artifact(origin, tmp_path, "art-1")
        bad = manifest_to_dict(manifest)
        del bad["head_sha"]
        artifacts.put_raw("art-1", bad, payload)
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_artifact(handle, "a1", "art-1", spec=spec(origin, base))
        assert exc.value.code == ARTIFACT_INVALID

    def test_payload_checksum_mismatch(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        workspaces: WorkspaceService,
        artifacts: InMemoryArtifactStore,
    ) -> None:
        origin, base = make_repo(tmp_path)
        manifest, _ = make_patch_artifact(origin, tmp_path, "art-1")
        artifacts.put(manifest, b"tampered payload")
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_artifact(handle, "a1", "art-1", spec=spec(origin, base))
        assert exc.value.code == CHECKSUM_MISMATCH
        # Nothing was applied or even cloned.
        assert workspaces.get("a1") is None

    def test_missing_payload_is_invalid(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        artifacts: InMemoryArtifactStore,
    ) -> None:
        origin, base = make_repo(tmp_path)
        manifest, _ = make_patch_artifact(origin, tmp_path, "art-1")
        artifacts.put_raw("art-1", manifest_to_dict(manifest))
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_artifact(handle, "a1", "art-1", spec=spec(origin, base))
        assert exc.value.code == ARTIFACT_INVALID

    def test_manifest_base_mismatch(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        artifacts: InMemoryArtifactStore,
    ) -> None:
        origin, base = make_repo(tmp_path)
        manifest, payload = make_patch_artifact(origin, tmp_path, "art-1")
        wrong = ArtifactManifest(
            artifact_id=manifest.artifact_id,
            kind=manifest.kind,
            repo=manifest.repo,
            base_sha=SHA_0,
            head_sha=manifest.head_sha,
            payload_sha256=manifest.payload_sha256,
            files=manifest.files,
        )
        artifacts.put(wrong, payload)
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_artifact(handle, "a1", "art-1", spec=spec(origin, base))
        assert exc.value.code == BASE_SHA_MISMATCH
        assert not (handle.root / "repo" / "b.txt").exists()

    def test_repo_mismatch(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        artifacts: InMemoryArtifactStore,
    ) -> None:
        origin, base = make_repo(tmp_path)
        manifest, payload = make_patch_artifact(origin, tmp_path, "art-1")
        wrong = ArtifactManifest(
            artifact_id=manifest.artifact_id,
            kind=manifest.kind,
            repo="/elsewhere",
            base_sha=manifest.base_sha,
            head_sha=manifest.head_sha,
            payload_sha256=manifest.payload_sha256,
        )
        artifacts.put(wrong, payload)
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_artifact(handle, "a1", "art-1", spec=spec(origin, base))
        assert exc.value.code == ARTIFACT_INVALID

    def test_unapplying_patch(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        artifacts: InMemoryArtifactStore,
    ) -> None:
        origin, base = make_repo(tmp_path)
        payload = b"this is not a unified diff\n"
        manifest = ArtifactManifest(
            artifact_id="art-bad",
            kind=ARTIFACT_KIND_PATCH,
            repo=str(origin),
            base_sha=base,
            head_sha="1" * 40,
            payload_sha256=sha256(payload),
        )
        artifacts.put(manifest, payload)
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_artifact(handle, "a1", "art-bad", spec=spec(origin, base))
        assert exc.value.code == ARTIFACT_INVALID

    def test_bundle_without_head(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        artifacts: InMemoryArtifactStore,
    ) -> None:
        origin, base = make_repo(tmp_path)
        manifest, payload = make_bundle_artifact(origin, tmp_path, "art-1")
        wrong = ArtifactManifest(
            artifact_id=manifest.artifact_id,
            kind=manifest.kind,
            repo=manifest.repo,
            base_sha=manifest.base_sha,
            head_sha="1" * 40,
            payload_sha256=manifest.payload_sha256,
        )
        artifacts.put(wrong, payload)
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_artifact(handle, "a1", "art-1", spec=spec(origin, base))
        assert exc.value.code == HEAD_SHA_MISMATCH

    def test_drifted_workdir_rejected(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        workspaces: WorkspaceService,
        artifacts: InMemoryArtifactStore,
    ) -> None:
        origin, base = make_repo(tmp_path)
        manifest, payload = make_patch_artifact(origin, tmp_path, "art-1")
        artifacts.put(manifest, payload)
        workspaces.prepare(handle, "a1", spec(origin, base))
        workdir = handle.root / "repo"
        (workdir / "local.txt").write_text("x\n", encoding="utf-8")
        host_git(workdir, "add", "-A")
        host_git(workdir, "commit", "-qm", "local drift")
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_artifact(handle, "a1", "art-1")
        assert exc.value.code == BASE_SHA_MISMATCH
