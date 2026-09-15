"""SOR-83 glue: sandbox snapshot → durable artifact → B2 handoff bridge."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
from control.artifact_ops import (
    BUNDLE_MEMBER,
    HandoffStoreView,
    collect_sandbox_workspace,
    credential_forbidden_values,
    snapshot_workspace_artifact,
)
from control.artifacts import (
    ArtifactSecretError,
    InMemoryArtifactStore,
    WorkspacePolicy,
)
from control.backend import LocalProcessBackend, SandboxHandle, SandboxSpec
from control.handoff import HandoffService
from control.workspace import (
    BASE_SHA_MISMATCH,
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


@pytest.fixture
def store() -> InMemoryArtifactStore:
    return InMemoryArtifactStore()


def _workdir(handle: SandboxHandle) -> Path:
    return Path(handle.root) / "repo"


def _commit_in_workdir(handle: SandboxHandle, name: str, content: str) -> str:
    workdir = _workdir(handle)
    (workdir / name).write_text(content, encoding="utf-8")
    host_git(workdir, "add", "-A")
    host_git(workdir, "commit", "-qm", f"add {name}")
    return host_git(workdir, "rev-parse", "HEAD")


class TestCollectSandboxWorkspace:
    def test_collects_allowed_files(
        self, tmp_path: Path, backend: LocalProcessBackend, handle: SandboxHandle
    ) -> None:
        workdir = _workdir(handle)
        workdir.mkdir()
        (workdir / "src").mkdir()
        (workdir / "src" / "app.py").write_text("print('x')\n", encoding="utf-8")
        (workdir / ".env").write_text("TOKEN=REDACTED\n", encoding="utf-8")
        (workdir / "link").symlink_to(workdir / "src" / "app.py")
        files, members, warnings, excluded = collect_sandbox_workspace(backend, handle, "repo")
        paths = {f.path for f in files}
        assert paths == {"src/app.py"}
        assert members["files/src/app.py"] == b"print('x')\n"
        assert ".env" in excluded
        assert any("symlink" in w for w in warnings)

    def test_forbidden_value_fails_closed(
        self, tmp_path: Path, backend: LocalProcessBackend, handle: SandboxHandle
    ) -> None:
        workdir = _workdir(handle)
        workdir.mkdir()
        canary = b"sk-canary-REDACTED"
        (workdir / "leak.txt").write_bytes(b"prefix " + canary + b" suffix")
        with pytest.raises(ArtifactSecretError):
            collect_sandbox_workspace(
                backend,
                handle,
                "repo",
                policy=WorkspacePolicy(forbidden_values=(canary,)),
            )


class TestSnapshot:
    def test_snapshot_persists_members_and_checksums(
        self,
        tmp_path: Path,
        backend: LocalProcessBackend,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        store: InMemoryArtifactStore,
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", WorkspaceSpec(str(origin), "main", base))
        head = _commit_in_workdir(handle, "b.txt", "two\n")
        manifest = snapshot_workspace_artifact(
            backend=backend,
            handle=handle,
            workspaces=workspaces,
            store=store,
            agent_id="a1",
            run_id="run-1",
        )
        assert manifest.base_sha == base
        assert manifest.head_sha == head
        assert manifest.repo == str(origin)
        assert {f.path for f in manifest.files} == {"a.txt", "b.txt"}
        # Clean committed head → bundle pins the exact commit; patch carries
        # the content delta either way.
        assert BUNDLE_MEMBER in manifest.payloads
        assert "patch.diff" in manifest.payloads
        patch = store.read(manifest.artifact_id, "patch.diff")
        assert b"b.txt" in patch and b"two" in patch
        bundle = store.read(manifest.artifact_id, BUNDLE_MEMBER)
        assert hashlib.sha256(bundle).hexdigest() == manifest.payloads[BUNDLE_MEMBER]
        # Head moved on the durable record too.
        assert workspaces.get("a1").head_sha == head

    def test_dirty_tree_snapshot_has_patch_only(
        self,
        tmp_path: Path,
        backend: LocalProcessBackend,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        store: InMemoryArtifactStore,
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", WorkspaceSpec(str(origin), "main", base))
        (_workdir(handle) / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        manifest = snapshot_workspace_artifact(
            backend=backend,
            handle=handle,
            workspaces=workspaces,
            store=store,
            agent_id="a1",
        )
        assert manifest.head_sha == base  # HEAD never moved
        assert BUNDLE_MEMBER not in manifest.payloads
        assert b"dirty.txt" in store.read(manifest.artifact_id, "patch.diff")
        assert store.read(manifest.artifact_id, "files/dirty.txt") == b"uncommitted\n"

    def test_ignored_files_stay_out_of_manifest(
        self,
        tmp_path: Path,
        backend: LocalProcessBackend,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        store: InMemoryArtifactStore,
    ) -> None:
        """Gitignored test/build output is not transportable downstream — it
        must not enter the manifest, or consumer checksum verification can
        never pass."""
        origin, base = make_repo(tmp_path)
        (origin / ".gitignore").write_text("__pycache__/\n*.log\n", encoding="utf-8")
        host_git(origin, "add", "-A")
        host_git(origin, "commit", "-qm", "ignore rules")
        base = host_git(origin, "rev-parse", "HEAD")
        workspaces.prepare(handle, "a1", WorkspaceSpec(str(origin), "main", base))
        workdir = _workdir(handle)
        # Producer ran tests: generated junk lands in the worktree.
        (workdir / "__pycache__").mkdir()
        (workdir / "__pycache__" / "a.pyc").write_bytes(b"\x00junk")
        (workdir / "run.log").write_text("noise\n", encoding="utf-8")
        _commit_in_workdir(handle, "b.txt", "two\n")
        manifest = snapshot_workspace_artifact(
            backend=backend,
            handle=handle,
            workspaces=workspaces,
            store=store,
            agent_id="a1",
        )
        paths = {f.path for f in manifest.files}
        assert "__pycache__/a.pyc" not in paths and "run.log" not in paths
        assert {"a.txt", "b.txt", ".gitignore"} <= paths
        # The package must be consumable — the ignored files are absent
        # downstream and therefore absent from verification.
        consumer = backend.create(SandboxSpec())
        cws = WorkspaceService(backend, InMemoryWorkspaceStore())
        handoffs = HandoffService(cws, HandoffStoreView(store))
        record = handoffs.prepare_from_artifact(
            consumer, "cons", manifest.artifact_id, spec=WorkspaceSpec(str(origin), "main", base)
        )
        cdir = Path(consumer.root) / "repo"
        assert (cdir / "b.txt").read_text() == "two\n"
        assert not (cdir / "__pycache__").exists()
        assert record.head_sha == manifest.head_sha
        backend.terminate(consumer)

    def test_empty_patch_handoff_is_a_noop(
        self,
        tmp_path: Path,
        backend: LocalProcessBackend,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        store: InMemoryArtifactStore,
    ) -> None:
        """A no-change artifact (empty patch.diff, no bundle) must apply as
        a no-op — ``git apply`` rejects empty input, so the handoff skips
        the apply step for an empty payload."""
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", WorkspaceSpec(str(origin), "main", base))
        manifest = snapshot_workspace_artifact(
            backend=backend,
            handle=handle,
            workspaces=workspaces,
            store=store,
            agent_id="a1",
        )
        assert manifest.head_sha == base
        assert store.read(manifest.artifact_id, "patch.diff") == b""
        consumer = backend.create(SandboxSpec())
        cws = WorkspaceService(backend, InMemoryWorkspaceStore())
        handoffs = HandoffService(cws, HandoffStoreView(store))
        record = handoffs.prepare_from_artifact(
            consumer, "cons", manifest.artifact_id, spec=WorkspaceSpec(str(origin), "main", base)
        )
        assert record.head_sha == base
        backend.terminate(consumer)

    def test_embedded_repo_does_not_break_snapshot(
        self,
        tmp_path: Path,
        backend: LocalProcessBackend,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        store: InMemoryArtifactStore,
    ) -> None:
        """A nested ``git init`` (no commit) used to hard-fail ``git add -N``
        — and its contents could never survive an apply anyway."""
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", WorkspaceSpec(str(origin), "main", base))
        workdir = _workdir(handle)
        host_git(workdir, "init", "-q", "-b", "main", "vendored")
        (workdir / "vendored" / "inner.py").write_text("x = 1\n", encoding="utf-8")
        # A commitless embedded repo makes `git add -A` fail — stage the file
        # directly, the way an agent working around it would.
        (workdir / "b.txt").write_text("two\n", encoding="utf-8")
        host_git(workdir, "add", "b.txt")
        host_git(workdir, "commit", "-qm", "add b.txt")
        manifest = snapshot_workspace_artifact(
            backend=backend,
            handle=handle,
            workspaces=workspaces,
            store=store,
            agent_id="a1",
        )
        assert not any(f.path.startswith("vendored") for f in manifest.files)
        consumer = backend.create(SandboxSpec())
        cws = WorkspaceService(backend, InMemoryWorkspaceStore())
        handoffs = HandoffService(cws, HandoffStoreView(store))
        record = handoffs.prepare_from_artifact(
            consumer, "cons", manifest.artifact_id, spec=WorkspaceSpec(str(origin), "main", base)
        )
        # Dirty tree (untracked embedded repo) → patch path → a fresh local
        # commit carries the delta; the embedded content stays behind.
        assert record.head_sha != base
        cdir = Path(consumer.root) / "repo"
        assert (cdir / "b.txt").read_text() == "two\n"
        assert not (cdir / "vendored").exists()
        backend.terminate(consumer)

    def test_no_workspace_record(
        self,
        backend: LocalProcessBackend,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        store: InMemoryArtifactStore,
    ) -> None:
        with pytest.raises(WorkspaceError):
            snapshot_workspace_artifact(
                backend=backend,
                handle=handle,
                workspaces=workspaces,
                store=store,
                agent_id="a1",
            )

    def test_attaches_ref_to_durable_run(
        self,
        tmp_path: Path,
        backend: LocalProcessBackend,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        store: InMemoryArtifactStore,
    ) -> None:
        from control.run_store import InMemoryRunStore, RunLedger

        ledger = RunLedger(InMemoryRunStore())
        ledger.begin(agent_id="a1", n=1)
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", WorkspaceSpec(str(origin), "main", base))
        _commit_in_workdir(handle, "b.txt", "two\n")
        manifest = snapshot_workspace_artifact(
            backend=backend,
            handle=handle,
            workspaces=workspaces,
            store=store,
            agent_id="a1",
            run_id="run-1",
            ledger=ledger,
            run_n=1,
        )
        assert f"artifact://{manifest.artifact_id}" in ledger.get("a1", 1).artifact_refs


class TestHandoffStoreView:
    def test_patch_round_trip_into_b2_consume(
        self,
        tmp_path: Path,
        backend: LocalProcessBackend,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        store: InMemoryArtifactStore,
    ) -> None:
        """A B1 snapshot consumed through the view applies like a B2 package."""
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "prod", WorkspaceSpec(str(origin), "main", base))
        _commit_in_workdir(handle, "b.txt", "two\n")
        manifest = snapshot_workspace_artifact(
            backend=backend,
            handle=handle,
            workspaces=workspaces,
            store=store,
            agent_id="prod",
        )
        consumer = backend.create(SandboxSpec())
        cws = WorkspaceService(backend, InMemoryWorkspaceStore())
        handoffs = HandoffService(cws, HandoffStoreView(store))
        record = handoffs.prepare_from_artifact(
            consumer,
            "cons",
            manifest.artifact_id,
            spec=WorkspaceSpec(str(origin), "main", base),
        )
        cdir = Path(consumer.root) / "repo"
        assert (cdir / "b.txt").read_text() == "two\n"
        # The bundle pinned the producer's exact commit.
        assert record.head_sha == manifest.head_sha == record.checkout_sha
        backend.terminate(consumer)

    def test_unknown_and_malformed(self, store: InMemoryArtifactStore) -> None:
        view = HandoffStoreView(store)
        assert view.get_manifest("nope") is None
        assert view.read_payload("nope") is None


class TestCredentialForbiddenValues:
    def test_blob_contents_and_env(self) -> None:
        blob = {"provider": "codex", "files": {"auth.json": '{"token":"REDACTED"}'}}
        values = credential_forbidden_values(
            blob, env={"CODEX_AUTH_JSON": "secret-REDACTED", "PATH": "/bin"}
        )
        assert b'{"token":"REDACTED"}' in values
        assert b"secret-REDACTED" in values
        assert not any(b"/bin" == v for v in values)

    def test_provider_api_key_is_forbidden(self) -> None:
        """``SBX_PROVIDER_API_KEY`` is forwarded into codex sandboxes — a
        workspace file that captured it must fail the snapshot closed."""
        values = credential_forbidden_values(
            None,
            env={"SBX_PROVIDER_API_KEY": "sk-provider-REDACTED", "PATH": "/bin"},
        )
        assert b"sk-provider-REDACTED" in values
        assert not any(b"/bin" == v for v in values)


class TestWrongBase:
    def test_snapshot_base_is_checkout_not_head(
        self,
        tmp_path: Path,
        backend: LocalProcessBackend,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        store: InMemoryArtifactStore,
    ) -> None:
        """Chained handoff: artifact B's declared base is the checkout A's
        artifact left behind (A's head) — a consumer standing anywhere else
        fails ``base_sha_mismatch``."""
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "prod", WorkspaceSpec(str(origin), "main", base))
        head_a = _commit_in_workdir(handle, "b.txt", "two\n")
        m1 = snapshot_workspace_artifact(
            backend=backend,
            handle=handle,
            workspaces=workspaces,
            store=store,
            agent_id="prod",
        )
        # B consumes A's artifact — B's checkout_sha becomes A's head.
        consumer = backend.create(SandboxSpec())
        cws = WorkspaceService(backend, InMemoryWorkspaceStore())
        handoffs = HandoffService(cws, HandoffStoreView(store))
        handoffs.prepare_from_artifact(
            consumer,
            "cons",
            m1.artifact_id,
            spec=WorkspaceSpec(str(origin), "main", base),
        )
        cdir = Path(consumer.root) / "repo"
        (cdir / "c.txt").write_text("three\n", encoding="utf-8")
        host_git(cdir, "add", "-A")
        host_git(cdir, "commit", "-qm", "add c.txt")
        m2 = snapshot_workspace_artifact(
            backend=backend,
            handle=consumer,
            workspaces=cws,
            store=store,
            agent_id="cons",
        )
        assert m2.base_sha == head_a
        # A third workspace declaring the ORIGINAL base cannot consume B's
        # artifact — an explicit mismatch, never a silent wrong-base apply.
        orphan = backend.create(SandboxSpec())
        ows = WorkspaceService(backend, InMemoryWorkspaceStore())
        orphan_handoffs = HandoffService(ows, HandoffStoreView(store))
        with pytest.raises(WorkspaceError) as exc:
            orphan_handoffs.prepare_from_artifact(
                orphan,
                "late",
                m2.artifact_id,
                spec=WorkspaceSpec(str(origin), "main", base),
            )
        assert exc.value.code == BASE_SHA_MISMATCH
        backend.terminate(consumer)
        backend.terminate(orphan)
