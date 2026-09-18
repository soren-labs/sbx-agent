"""SOR-127 environment build / snapshot cache.

Deterministic coverage: explicit key/invalidation, last-known-good build
semantics (a failed build never replaces a healthy build), credential-free
build sandboxes, and the fail-closed ``base_sha`` gate on restored
environments. The Modal provider is exercised by source assertions only —
``make test`` never imports ``modal``.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from control.backend import LocalProcessBackend, SandboxHandle, SandboxSpec
from control.environment import (
    ENV_BUILD_TAG,
    ENV_FAILED,
    ENV_HEALTHY,
    EnvironmentBuildRecord,
    EnvironmentService,
    EnvironmentSpec,
    FileEnvironmentStore,
    InMemoryEnvironmentStore,
    LocalSnapshotProvider,
    build_env,
    env_record_from_dict,
    env_record_to_dict,
    environment_key,
    image_name_for,
)
from control.service import ControlPlane
from control.store import InMemoryStore
from control.workspace import (
    BASE_SHA_MISMATCH,
    CHECKOUT_FAILED,
    REPO_UNAVAILABLE,
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

SHA_A = "a" * 40
SHA_B = "b" * 40
KEY_C = "c" * 64


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


class SpyBackend(LocalProcessBackend):
    """Records ``create`` specs and per-exec argv/env for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.created: list[SandboxSpec] = []
        self.execs: list[tuple[list[str], dict[str, str]]] = []

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        self.created.append(spec)
        return super().create(spec)

    def exec(self, handle, argv, env=None):  # noqa: ANN001
        self.execs.append((list(argv), dict(env or {})))
        return super().exec(handle, argv, env=env)


@pytest.fixture
def backend() -> SpyBackend:
    return SpyBackend()


@pytest.fixture
def snapshots(backend: SpyBackend, tmp_path: Path) -> LocalSnapshotProvider:
    return LocalSnapshotProvider(backend, tmp_path / "env-snapshots")


@pytest.fixture
def store() -> InMemoryEnvironmentStore:
    return InMemoryEnvironmentStore()


@pytest.fixture
def envs(
    backend: SpyBackend, store: InMemoryEnvironmentStore, snapshots: LocalSnapshotProvider
) -> EnvironmentService:
    return EnvironmentService(backend, store, snapshots=snapshots)


def spec(repo: Path, base_sha: str, **kwargs) -> EnvironmentSpec:
    return EnvironmentSpec(
        repo=str(repo), base_ref="main", base_sha=base_sha, image="img", **kwargs
    )


def stub_runner() -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve().parents[3] / "tests" / "fakes" / "stub_runner.py"),
    ]


class TestSpec:
    def test_valid(self) -> None:
        s = EnvironmentSpec(repo="/r", base_ref="main", base_sha=SHA_A)
        assert (s.provider, s.workdir, s.image, s.setup) == ("codex", "repo", "", "")

    @pytest.mark.parametrize("field", ["repo", "base_ref", "provider"])
    def test_empty_required(self, field: str) -> None:
        kwargs = {
            "repo": "/r",
            "base_ref": "main",
            "base_sha": SHA_A,
            "provider": "codex",
            field: "",
        }
        with pytest.raises(WorkspaceError) as exc:
            EnvironmentSpec(**kwargs)
        assert exc.value.code == WORKSPACE_INVALID

    @pytest.mark.parametrize("sha", ["", "abc", "A" * 40, "g" * 40, "a" * 64])
    def test_bad_base_sha(self, sha: str) -> None:
        with pytest.raises(WorkspaceError) as exc:
            EnvironmentSpec(repo="/r", base_ref="main", base_sha=sha)
        assert exc.value.code == WORKSPACE_INVALID

    @pytest.mark.parametrize("workdir", ["", "/abs", "../x", "a/../b"])
    def test_unsafe_workdir(self, workdir: str) -> None:
        with pytest.raises(WorkspaceError) as exc:
            EnvironmentSpec(repo="/r", base_ref="main", base_sha=SHA_A, workdir=workdir)
        assert exc.value.code == WORKSPACE_INVALID


class TestKey:
    def test_deterministic(self) -> None:
        s1 = EnvironmentSpec(repo="/r", base_ref="main", base_sha=SHA_A, image="i")
        s2 = EnvironmentSpec(repo="/r", base_ref="main", base_sha=SHA_A, image="i")
        assert s1.key == s2.key == environment_key(s1)
        assert len(s1.key) == 64

    @pytest.mark.parametrize(
        "field,value",
        [
            ("repo", "/other"),
            ("base_ref", "dev"),
            ("base_sha", SHA_B),
            ("provider", "devin"),
            ("workdir", "src"),
            ("image", "other-img"),
            ("setup", "make deps"),
        ],
    )
    def test_every_input_invalidates(self, field: str, value: str) -> None:
        base = EnvironmentSpec(repo="/r", base_ref="main", base_sha=SHA_A)
        kwargs = {"repo": "/r", "base_ref": "main", "base_sha": SHA_A, field: value}
        changed = EnvironmentSpec(**kwargs)
        assert base.key != changed.key


class TestRecordCodec:
    def test_roundtrip(self) -> None:
        record = EnvironmentBuildRecord(
            key="c" * 64,
            repo="/r",
            base_ref="main",
            base_sha=SHA_A,
            provider="devin",
            workdir="src",
            image="img",
            setup="make",
            status=ENV_HEALTHY,
            snapshot_ref="im-123",
            attempts=3,
            last_error="x",
            created_at="t0",
            updated_at="t1",
            built_at="t2",
            failed_at="t3",
        )
        assert env_record_from_dict(env_record_to_dict(record)) == record

    @pytest.mark.parametrize(
        "patch",
        [
            {"status": "bogus"},
            {"snapshot_ref": "bad ref!"},
            {"attempts": True},
            {"attempts": -1},
            {"base_sha": "zz"},
            {"workdir": "../escape"},
            {"key": ""},
            {"repo": None},
        ],
    )
    def test_strict_decode(self, patch: dict) -> None:
        data = env_record_to_dict(
            EnvironmentBuildRecord(key="c" * 64, repo="/r", base_ref="main", base_sha=SHA_A)
        )
        data.update(patch)
        with pytest.raises(ValueError):
            env_record_from_dict(data)


class TestStores:
    def test_inmemory_roundtrip(self, store: InMemoryEnvironmentStore) -> None:
        record = EnvironmentBuildRecord(key="c" * 64, repo="/r", base_ref="main", base_sha=SHA_A)
        assert store.get(record.key) is None
        store.put(record)
        assert store.get(record.key) == record
        assert store.list() == [record]
        store.delete(record.key)
        assert store.get(record.key) is None

    def test_file_store_persists(self, tmp_path: Path) -> None:
        store = FileEnvironmentStore(tmp_path / "envs")
        record = EnvironmentBuildRecord(
            key="c" * 64,
            repo="/r",
            base_ref="main",
            base_sha=SHA_A,
            status=ENV_HEALTHY,
            snapshot_ref="ref-1",
        )
        store.put(record)
        assert FileEnvironmentStore(tmp_path / "envs").get(record.key) == record
        assert store.list() == [record]

    def test_file_store_corrupt(self, tmp_path: Path) -> None:
        root = tmp_path / "envs"
        root.mkdir()
        (root / f"{KEY_C}.json").write_text("not json", encoding="utf-8")
        with pytest.raises(WorkspaceError) as exc:
            FileEnvironmentStore(root).get(KEY_C)
        assert exc.value.code == WORKSPACE_INVALID


class TestBuildEnv:
    def test_never_forwards_credentials(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CODEX_AUTH_JSON", "REDACTED")
        monkeypatch.setenv(
            "SBX_ACCOUNT_CREDENTIAL",
            json.dumps({"provider": "codex", "files": {"x": "REDACTED"}}),
        )
        monkeypatch.setenv("SBX_PROVIDER_API_KEY", "REDACTED")
        handle = SandboxHandle(id="sb", root=tmp_path, tags={"provider": "codex"})
        env = build_env(handle)
        for key in (
            "CODEX_AUTH_JSON",
            "SBX_ACCOUNT_CREDENTIAL",
            "SBX_PROVIDER_API_KEY",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        ):
            assert key not in env
        assert env["SBX_WORK"] == str(tmp_path)
        assert env["HOME"] == str(tmp_path / "home")
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_extra_cannot_smuggle_git_credential_channels(self, tmp_path: Path) -> None:
        handle = SandboxHandle(id="sb", root=tmp_path, tags={})
        env = build_env(handle, {"GIT_ASKPASS": "/evil", "GH_TOKEN": "REDACTED", "OK": "1"})
        assert "GIT_ASKPASS" not in env
        assert "GH_TOKEN" not in env
        assert env["OK"] == "1"


class TestLocalSnapshotProvider:
    def test_roundtrip_preserves_files(
        self, backend: SpyBackend, snapshots: LocalSnapshotProvider
    ) -> None:
        handle = backend.create(SandboxSpec())
        (handle.root / "repo").mkdir()
        (handle.root / "repo" / "a.txt").write_text("one\n")
        (handle.root / ".hidden").write_text("dot\n")
        ref = snapshots.snapshot(handle)
        # Mutating the source after snapshot must not leak into restores.
        (handle.root / "repo" / "a.txt").write_text("changed\n")
        restored = snapshots.restore(ref, SandboxSpec(tags={"x": "1"}))
        assert restored.id != handle.id
        assert (restored.root / "repo" / "a.txt").read_text() == "one\n"
        assert (restored.root / ".hidden").read_text() == "dot\n"

    def test_restore_missing_ref(
        self, backend: SpyBackend, snapshots: LocalSnapshotProvider
    ) -> None:
        with pytest.raises(WorkspaceError) as exc:
            snapshots.restore("0" * 32, SandboxSpec())
        assert exc.value.code == REPO_UNAVAILABLE

    @pytest.mark.parametrize("ref", ["../escape", "a/b", "bad ref"])
    def test_restore_unsafe_ref(
        self, backend: SpyBackend, snapshots: LocalSnapshotProvider, ref: str
    ) -> None:
        with pytest.raises(WorkspaceError) as exc:
            snapshots.restore(ref, SandboxSpec())
        assert exc.value.code == WORKSPACE_INVALID

    def test_snapshot_nonlocal_root(self, snapshots: LocalSnapshotProvider) -> None:
        handle = SandboxHandle(id="sb", root=Path("/work"), tags={})
        # /work may or may not exist locally; force a definitely-remote root.
        if handle.root.is_dir():
            handle = SandboxHandle(id="sb", root=Path("/nonexistent-root"), tags={})
        with pytest.raises(WorkspaceError) as exc:
            snapshots.snapshot(handle)
        assert exc.value.code == WORKSPACE_INVALID


class TestBuild:
    def test_build_healthy(
        self,
        envs: EnvironmentService,
        backend: SpyBackend,
        tmp_path: Path,
    ) -> None:
        repo, base_sha = make_repo(tmp_path)
        record = envs.build(spec(repo, base_sha, setup="echo dep > .deps"))
        assert record.status == ENV_HEALTHY
        assert record.snapshot_ref
        assert record.attempts == 1
        assert record.last_error is None
        assert record.built_at
        # The builder sandbox is credential-free and torn down afterwards.
        assert backend.created[0].secrets == []
        assert backend.created[0].resource_secrets == []
        assert backend.created[0].tags[ENV_BUILD_TAG] == "1"
        assert backend.list() == []

    def test_build_base_mismatch_fails_closed(
        self, envs: EnvironmentService, tmp_path: Path
    ) -> None:
        repo, _ = make_repo(tmp_path)
        with pytest.raises(WorkspaceError) as exc:
            envs.build(spec(repo, SHA_A))
        assert exc.value.code == BASE_SHA_MISMATCH
        record = envs.get(spec(repo, SHA_A).key)
        assert record is not None
        assert record.status == ENV_FAILED
        assert record.snapshot_ref is None
        assert envs.resolve(spec(repo, SHA_A)) is None

    def test_build_clone_failure(self, envs: EnvironmentService, tmp_path: Path) -> None:
        missing = tmp_path / "no-such-repo"
        with pytest.raises(WorkspaceError) as exc:
            envs.build(spec(missing, SHA_A))
        assert exc.value.code == REPO_UNAVAILABLE
        record = envs.get(spec(missing, SHA_A).key)
        assert record is not None and record.status == ENV_FAILED
        assert record.attempts == 1 and record.last_error

    def test_build_setup_failure(self, envs: EnvironmentService, tmp_path: Path) -> None:
        repo, base_sha = make_repo(tmp_path)
        with pytest.raises(WorkspaceError) as exc:
            envs.build(spec(repo, base_sha, setup="exit 3"))
        assert exc.value.code == CHECKOUT_FAILED
        record = envs.get(spec(repo, base_sha, setup="exit 3").key)
        assert record is not None and record.status == ENV_FAILED

    def test_failed_build_never_replaces_healthy(
        self, envs: EnvironmentService, tmp_path: Path
    ) -> None:
        repo, base_sha = make_repo(tmp_path)
        env_spec = spec(repo, base_sha)
        good = envs.build(env_spec)
        # Break the world: the repo can no longer be cloned.
        repo.rename(tmp_path / "gone")
        with pytest.raises(WorkspaceError):
            envs.build(env_spec)
        record = envs.get(env_spec.key)
        assert record is not None
        # Last-known-good: the healthy snapshot still serves; the failure is
        # recorded, attempts counted, status stays healthy.
        assert record.status == ENV_HEALTHY
        assert record.snapshot_ref == good.snapshot_ref
        assert record.attempts == 2
        assert record.last_error
        assert envs.resolve(env_spec).snapshot_ref == good.snapshot_ref

    def test_rebuild_after_failure(self, envs: EnvironmentService, tmp_path: Path) -> None:
        repo, base_sha = make_repo(tmp_path)
        env_spec = spec(repo, base_sha)
        with pytest.raises(WorkspaceError):
            envs.build(spec(tmp_path / "missing", base_sha))
        record = envs.build(env_spec)
        assert record.status == ENV_HEALTHY and record.snapshot_ref
        assert envs.resolve(env_spec).snapshot_ref == record.snapshot_ref

    def test_build_without_provider_fails(
        self, backend: SpyBackend, store: InMemoryEnvironmentStore, tmp_path: Path
    ) -> None:
        repo, base_sha = make_repo(tmp_path)
        envs = EnvironmentService(backend, store, snapshots=None)
        with pytest.raises(WorkspaceError) as exc:
            envs.build(spec(repo, base_sha))
        assert exc.value.code == WORKSPACE_INVALID
        assert backend.list() == []

    def test_try_build_never_raises(self, envs: EnvironmentService, tmp_path: Path) -> None:
        record = envs.try_build(spec(tmp_path / "missing", SHA_A))
        assert record.status == ENV_FAILED
        assert record.snapshot_ref is None

    def test_invalidate(self, envs: EnvironmentService, tmp_path: Path) -> None:
        repo, base_sha = make_repo(tmp_path)
        env_spec = spec(repo, base_sha)
        envs.build(env_spec)
        assert envs.resolve(env_spec) is not None
        envs.invalidate(env_spec.key)
        assert envs.resolve(env_spec) is None
        assert envs.get(env_spec.key) is None


class TestCredentialsNotCaptured:
    def test_build_execs_never_see_ambient_credentials(
        self,
        envs: EnvironmentService,
        backend: SpyBackend,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        monkeypatch.setenv("CODEX_AUTH_JSON", "REDACTED")
        monkeypatch.setenv("SBX_PROVIDER_API_KEY", "REDACTED")
        monkeypatch.setenv(
            "SBX_ACCOUNT_CREDENTIAL",
            json.dumps({"provider": "codex", "files": {".codex/auth.json": "REDACTED"}}),
        )
        repo, base_sha = make_repo(tmp_path)
        envs.build(spec(repo, base_sha, provider="codex"))
        for _argv, env in backend.execs:
            for key in (
                "CODEX_AUTH_JSON",
                "SBX_ACCOUNT_CREDENTIAL",
                "SBX_PROVIDER_API_KEY",
                "GH_TOKEN",
                "GITHUB_TOKEN",
            ):
                assert key not in env

    def test_scrub_removes_credential_paths(
        self,
        envs: EnvironmentService,
        snapshots: LocalSnapshotProvider,
        tmp_path: Path,
    ) -> None:
        repo, base_sha = make_repo(tmp_path)
        setup = (
            "mkdir -p $HOME/.grok $HOME/.ssh && "
            "echo tok > $HOME/.grok/auth.json && "
            "echo tok > $HOME/.ssh/id_rsa && "
            "echo cred > .git-credentials && "
            "mkdir -p $SBX_WORK/.codex && echo tok > $SBX_WORK/.codex/auth.json && "
            "echo events > $SBX_WORK/events.jsonl && "
            "echo dep > .deps"
        )
        record = envs.build(spec(repo, base_sha, setup=setup))
        restored = snapshots.restore(record.snapshot_ref, SandboxSpec())
        workdir = restored.root / "repo"
        assert (workdir / ".deps").read_text() == "dep\n"
        for rel in (
            "home/.grok/auth.json",
            "home/.ssh/id_rsa",
            "repo/.git-credentials",
            ".codex/auth.json",
            "events.jsonl",
        ):
            assert not (restored.root / rel).exists(), rel

    def test_origin_url_sanitized(
        self,
        envs: EnvironmentService,
        snapshots: LocalSnapshotProvider,
        tmp_path: Path,
    ) -> None:
        repo, base_sha = make_repo(tmp_path)
        record = envs.build(spec(repo, base_sha))
        restored = snapshots.restore(record.snapshot_ref, SandboxSpec())
        url = host_git(restored.root / "repo", "config", "--get", "remote.origin.url")
        assert "@" not in url


class TestRestoreAndPrepare:
    def _build(
        self, envs: EnvironmentService, tmp_path: Path, setup: str = "echo dep > .deps"
    ) -> tuple[Path, str, EnvironmentBuildRecord]:
        repo, base_sha = make_repo(tmp_path)
        record = envs.build(spec(repo, base_sha, setup=setup))
        return repo, base_sha, record

    def test_restore_then_prepare_restored(
        self,
        envs: EnvironmentService,
        snapshots: LocalSnapshotProvider,
        backend: SpyBackend,
        tmp_path: Path,
    ) -> None:
        repo, base_sha, record = self._build(envs, tmp_path)
        handle = snapshots.restore(record.snapshot_ref, SandboxSpec(tags={"provider": "codex"}))
        workspaces = WorkspaceService(backend, InMemoryWorkspaceStore())
        ws = workspaces.prepare_restored(
            handle, "agent-1", WorkspaceSpec(str(repo), "main", base_sha)
        )
        assert ws.prepared
        assert ws.checkout_sha == base_sha == ws.head_sha
        assert (handle.root / "repo" / ".deps").read_text() == "dep\n"
        assert host_git(handle.root / "repo", "rev-parse", "HEAD") == base_sha

    def test_prepare_restored_fails_closed_on_drift(
        self,
        envs: EnvironmentService,
        snapshots: LocalSnapshotProvider,
        backend: SpyBackend,
        tmp_path: Path,
    ) -> None:
        repo, base_sha, record = self._build(envs, tmp_path)
        handle = snapshots.restore(record.snapshot_ref, SandboxSpec())
        host_git(handle.root / "repo", "commit", "-qm", "drift", "--allow-empty")
        workspaces = WorkspaceService(backend, InMemoryWorkspaceStore())
        with pytest.raises(WorkspaceError) as exc:
            workspaces.prepare_restored(
                handle, "agent-1", WorkspaceSpec(str(repo), "main", base_sha)
            )
        assert exc.value.code == BASE_SHA_MISMATCH
        record_ws = workspaces.get("agent-1")
        assert record_ws is not None and not record_ws.prepared

    def test_prepare_restored_git_policy_branch(
        self,
        envs: EnvironmentService,
        snapshots: LocalSnapshotProvider,
        backend: SpyBackend,
        tmp_path: Path,
    ) -> None:
        repo, base_sha, record = self._build(envs, tmp_path)
        handle = snapshots.restore(record.snapshot_ref, SandboxSpec())
        workspaces = WorkspaceService(backend, InMemoryWorkspaceStore())
        ws = workspaces.prepare_restored(
            handle,
            "agent-1",
            WorkspaceSpec(str(repo), "main", base_sha),
            git={"branch": "feat/work", "push": False},
        )
        assert ws.branch == "feat/work"
        assert host_git(handle.root / "repo", "rev-parse", "feat/work") == base_sha


class TestProvisionSeam:
    def test_provision_restores_via_provider(self, tmp_path: Path) -> None:
        backend = LocalProcessBackend()
        store = InMemoryStore()
        plane = ControlPlane(backend, store, stub_runner())
        provider = LocalSnapshotProvider(backend, tmp_path / "snaps")
        source = backend.create(SandboxSpec())
        (source.root / "marker").write_text("snap\n")
        ref = provider.snapshot(source)
        plane.snapshot_provider = provider

        session_id = plane.open_session(owner="o", title=None, model=None)
        plane.provision_session(session_id, env_snapshot=ref)
        rec = store.get(session_id)
        assert rec is not None and rec.status == "idle"
        handle = rec.handle()
        assert handle is not None
        assert (handle.root / "marker").read_text() == "snap\n"
        plane.close(session_id)

    def test_provision_without_provider_fails_closed(self) -> None:
        backend = LocalProcessBackend()
        store = InMemoryStore()
        plane = ControlPlane(backend, store, stub_runner())
        session_id = plane.open_session(owner="o", title=None, model=None)
        with pytest.raises(RuntimeError):
            plane.provision_session(session_id, env_snapshot="ref-1")
        rec = store.get(session_id)
        assert rec is not None and rec.status == "lost"

    def test_prepare_workspace_restored_path(self, tmp_path: Path) -> None:
        """``_prepare_workspace(env_restored=True)`` skips the clone and
        verifies the snapshot's HEAD against the declared base_sha."""
        from control.api_v1.lifecycle import _prepare_workspace

        backend = LocalProcessBackend()
        store = InMemoryStore()
        plane = ControlPlane(backend, store, stub_runner())
        provider = LocalSnapshotProvider(backend, tmp_path / "snaps")
        envs = EnvironmentService(backend, InMemoryEnvironmentStore(), snapshots=provider)
        plane.environments = envs
        plane.snapshot_provider = provider
        plane.workspaces = WorkspaceService(backend, InMemoryWorkspaceStore())

        repo, base_sha = make_repo(tmp_path)
        record = envs.build(spec(repo, base_sha, setup="echo dep > .deps"))
        session_id = plane.open_session(owner="o", title=None, model=None)
        plane.provision_session(session_id, env_snapshot=record.snapshot_ref)
        _prepare_workspace(
            plane,
            session_id,
            {"repo": str(repo), "base_ref": "main", "base_sha": base_sha},
            None,
            env_restored=True,
        )
        ws = plane.workspaces.get(session_id)
        assert ws is not None and ws.prepared and ws.checkout_sha == base_sha
        handle = plane.get(session_id).handle()
        assert (handle.root / "repo" / ".deps").read_text() == "dep\n"
        plane.close(session_id)


class TestModalSeam:
    """Static guarantees for the real-Modal path — no ``import modal`` in tests."""

    MODAL_SRC = Path(__file__).resolve().parents[3] / "control" / "backends" / "modal.py"
    ENV_SRC = Path(__file__).resolve().parents[3] / "control" / "environment.py"

    def test_modal_provider_uses_native_snapshot_primitives(self) -> None:
        src = self.MODAL_SRC.read_text(encoding="utf-8")
        assert "snapshot_filesystem" in src
        assert "Image.from_id" in src or "from_id" in src
        assert "ModalSnapshotProvider" in src
        assert "_create_with_image" in src
        # Build sandboxes never carry Secrets — create or exec.
        assert "_is_env_build" in src
        assert ENV_BUILD_TAG in src

    def test_no_top_level_modal_imports(self) -> None:
        for path in (self.MODAL_SRC, self.ENV_SRC):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.Import):
                    assert not any(n.name == "modal" for n in node.names)
                elif isinstance(node, ast.ImportFrom):
                    assert node.module != "modal"


class TestImageNames:
    def test_provider_images(self, monkeypatch) -> None:
        assert image_name_for("codex") == "sbx-runtime"
        assert image_name_for("devin") == "sbx-runtime-devin"
        monkeypatch.setenv("SBX_IMAGE_DEVIN", "custom-devin")
        assert image_name_for("devin") == "custom-devin"
