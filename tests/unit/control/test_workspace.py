"""Workspace contract: declaration, base_sha gate, recorded head, review pin."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from control.backend import LocalProcessBackend, SandboxHandle, SandboxSpec
from control.workspace import (
    BASE_SHA_MISMATCH,
    CHECKOUT_FAILED,
    HEAD_SHA_MISMATCH,
    REPO_UNAVAILABLE,
    WORKSPACE_INVALID,
    WORKSPACE_NOT_FOUND,
    FileWorkspaceStore,
    InMemoryWorkspaceStore,
    WorkspaceError,
    WorkspaceRecord,
    WorkspaceService,
    WorkspaceSpec,
    record_from_dict,
    record_to_dict,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "sbx-test",
    "GIT_AUTHOR_EMAIL": "sbx-test@localhost",
    "GIT_COMMITTER_NAME": "sbx-test",
    "GIT_COMMITTER_EMAIL": "sbx-test@localhost",
}

SHA_A = "a" * 40
SHA_0 = "0" * 40


def host_git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def make_repo(root: Path, name: str = "origin") -> tuple[Path, str]:
    """Repo with one commit on ``main``; returns (path, base_sha)."""
    repo = root / name
    repo.mkdir()
    host_git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    host_git(repo, "add", "-A")
    host_git(repo, "commit", "-qm", "A")
    return repo, host_git(repo, "rev-parse", "HEAD")


@pytest.fixture
def backend() -> LocalProcessBackend:
    return LocalProcessBackend()


@pytest.fixture
def handle(backend: LocalProcessBackend) -> SandboxHandle:
    return backend.create(SandboxSpec())


@pytest.fixture
def workspaces(backend: LocalProcessBackend) -> WorkspaceService:
    return WorkspaceService(backend, InMemoryWorkspaceStore())


def spec(repo: Path, base_sha: str, base_ref: str = "main") -> WorkspaceSpec:
    return WorkspaceSpec(repo=str(repo), base_ref=base_ref, base_sha=base_sha)


class TestSpec:
    def test_valid(self) -> None:
        s = WorkspaceSpec(repo="/r", base_ref="main", base_sha=SHA_A)
        assert (s.repo, s.base_ref, s.base_sha) == ("/r", "main", SHA_A)

    @pytest.mark.parametrize("field", ["repo", "base_ref"])
    def test_empty_required(self, field: str) -> None:
        kwargs = {"repo": "/r", "base_ref": "main", "base_sha": SHA_A, field: ""}
        with pytest.raises(WorkspaceError) as exc:
            WorkspaceSpec(**kwargs)
        assert exc.value.code == WORKSPACE_INVALID

    @pytest.mark.parametrize("sha", ["", "abc", "A" * 40, "g" * 40, "a" * 39, "a" * 64])
    def test_bad_base_sha(self, sha: str) -> None:
        with pytest.raises(WorkspaceError) as exc:
            WorkspaceSpec(repo="/r", base_ref="main", base_sha=sha)
        assert exc.value.code == WORKSPACE_INVALID


class TestRecordCodec:
    def test_roundtrip(self) -> None:
        record = WorkspaceRecord(
            agent_id="a1",
            repo="/r",
            base_ref="main",
            base_sha=SHA_A,
            workdir="src/ws",
            checkout_sha=SHA_A,
            head_sha=SHA_A,
            reviewed_head_sha=SHA_A,
            created_at="t0",
            updated_at="t1",
        )
        assert record_from_dict(record_to_dict(record)) == record

    @pytest.mark.parametrize(
        "data",
        [
            None,
            "x",
            {},
            {"agent_id": "a1"},
            {"agent_id": "a1", "repo": "/r", "base_ref": "main", "base_sha": "zz"},
            {
                "agent_id": "a1",
                "repo": "/r",
                "base_ref": "main",
                "base_sha": SHA_A,
                "head_sha": "notasha",
            },
        ],
    )
    def test_rejects_junk(self, data) -> None:
        with pytest.raises(ValueError):
            record_from_dict(data)


class TestStores:
    def _record(self) -> WorkspaceRecord:
        return WorkspaceRecord(agent_id="a1", repo="/r", base_ref="main", base_sha=SHA_A)

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_roundtrip_delete(self, tmp_path: Path, kind: str) -> None:
        store = InMemoryWorkspaceStore() if kind == "memory" else FileWorkspaceStore(tmp_path)
        assert store.get("a1") is None
        store.put(self._record())
        assert store.get("a1") == self._record()
        store.delete("a1")
        assert store.get("a1") is None
        store.delete("a1")  # idempotent

    def test_file_store_corrupt_is_explicit(self, tmp_path: Path) -> None:
        store = FileWorkspaceStore(tmp_path)
        path = tmp_path / "a1" / "workspace.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(WorkspaceError) as exc:
            store.get("a1")
        assert exc.value.code == WORKSPACE_INVALID

    def test_memory_store_corrupt_is_explicit(self) -> None:
        """Same contract as the file store: an undecodable stored payload
        surfaces as ``WorkspaceError(workspace_invalid)``, not a bare
        ``ValueError`` that escapes as a 500."""
        store = InMemoryWorkspaceStore()
        store._items["a1"] = {"agent_id": "a1"}  # undecodable: missing fields
        with pytest.raises(WorkspaceError) as exc:
            store.get("a1")
        assert exc.value.code == WORKSPACE_INVALID


class TestPrepare:
    def test_clone_checkout_records_actual(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        record = workspaces.prepare(handle, "a1", spec(origin, base))
        assert record.prepared
        assert record.checkout_sha == base
        assert record.head_sha == base
        assert record.reviewed_head_sha is None
        assert record.workdir == "repo"
        assert (handle.root / "repo" / "a.txt").read_text() == "one\n"
        assert host_git(handle.root / "repo", "rev-parse", "HEAD") == base
        assert workspaces.get("a1").checkout_sha == base

    def test_non_default_base_ref(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        host_git(origin, "checkout", "-qb", "work")
        (origin / "b.txt").write_text("two\n", encoding="utf-8")
        host_git(origin, "add", "-A")
        host_git(origin, "commit", "-qm", "B")
        work_sha = host_git(origin, "rev-parse", "HEAD")
        host_git(origin, "checkout", "-q", "main")
        record = workspaces.prepare(handle, "a1", spec(origin, work_sha, base_ref="work"))
        assert record.checkout_sha == work_sha != base

    def test_base_sha_mismatch_fails_loudly(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, _ = make_repo(tmp_path)
        with pytest.raises(WorkspaceError) as exc:
            workspaces.prepare(handle, "a1", spec(origin, SHA_0))
        assert exc.value.code == BASE_SHA_MISMATCH
        record = workspaces.get("a1")
        assert record is not None and not record.prepared

    def test_moved_ref_is_a_mismatch(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        host_git(origin, "commit", "-qm", "B", "--allow-empty")
        with pytest.raises(WorkspaceError) as exc:
            workspaces.prepare(handle, "a1", spec(origin, base))
        assert exc.value.code == BASE_SHA_MISMATCH

    def test_unresolvable_ref(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        with pytest.raises(WorkspaceError) as exc:
            workspaces.prepare(handle, "a1", spec(origin, base, base_ref="nosuch"))
        assert exc.value.code == CHECKOUT_FAILED

    def test_bad_repo(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        with pytest.raises(WorkspaceError) as exc:
            workspaces.prepare(handle, "a1", spec(tmp_path / "missing", SHA_A))
        assert exc.value.code == REPO_UNAVAILABLE

    def test_double_declare_rejected(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        with pytest.raises(WorkspaceError) as exc:
            workspaces.prepare(handle, "a1", spec(origin, base))
        assert exc.value.code == WORKSPACE_INVALID

    def test_drop_then_reprepare(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, _ = make_repo(tmp_path)
        with pytest.raises(WorkspaceError):
            workspaces.prepare(handle, "a1", spec(origin, SHA_0))
        workspaces.drop("a1")
        (handle.root / "repo").rename(handle.root / "repo.failed")
        origin2, base2 = make_repo(tmp_path, "origin2")
        record = workspaces.prepare(handle, "a1", spec(origin2, base2))
        assert record.checkout_sha == base2

    @pytest.mark.parametrize("workdir", ["", "../x", "/abs", "a/../b"])
    def test_bad_workdir(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService, workdir: str
    ) -> None:
        origin, base = make_repo(tmp_path)
        with pytest.raises(WorkspaceError) as exc:
            workspaces.prepare(handle, "a1", spec(origin, base), workdir=workdir)
        assert exc.value.code == WORKSPACE_INVALID


class TestHeadAndReview:
    def test_refresh_head_tracks_commits(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        workdir = handle.root / "repo"
        (workdir / "b.txt").write_text("two\n", encoding="utf-8")
        host_git(workdir, "add", "-A")
        host_git(workdir, "commit", "-qm", "B")
        head = host_git(workdir, "rev-parse", "HEAD")
        updated = workspaces.refresh_head(handle, "a1")
        assert updated.head_sha == head != base
        assert updated.checkout_sha == base

    def test_refresh_head_requires_prepared(
        self, workspaces: WorkspaceService, handle: SandboxHandle
    ) -> None:
        with pytest.raises(WorkspaceError) as exc:
            workspaces.refresh_head(handle, "missing")
        assert exc.value.code == WORKSPACE_NOT_FOUND

    def test_mark_reviewed_defaults_to_head(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        record = workspaces.mark_reviewed("a1")
        assert record.reviewed_head_sha == base

    def test_mark_reviewed_explicit_match(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        assert workspaces.mark_reviewed("a1", base).reviewed_head_sha == base

    def test_mark_reviewed_mismatch_rejected(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        with pytest.raises(WorkspaceError) as exc:
            workspaces.mark_reviewed("a1", SHA_0)
        assert exc.value.code == HEAD_SHA_MISMATCH
        assert workspaces.get("a1").reviewed_head_sha is None

    def test_mark_reviewed_bad_sha(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        with pytest.raises(WorkspaceError) as exc:
            workspaces.mark_reviewed("a1", "notasha")
        assert exc.value.code == WORKSPACE_INVALID
