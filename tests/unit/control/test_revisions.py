"""SOR-225: durable Revision / Review / Delivery / Merge lifecycle.

Covers the seams the issue gates on: materialization on every successful
code-changing run, delivery that works after the author sandbox is gone,
durable review records with stale/independence semantics, and the merge
fail-closed chain.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from control.artifacts import InMemoryArtifactStore
from control.backend import LocalProcessBackend, SandboxHandle, SandboxSpec
from control.github_remote import RemoteGitHubError
from control.revisions import (
    DELIVERY_NOT_FOUND,
    INDEPENDENCE_VIOLATION,
    REVISION_NOT_READY,
    FileRevisionStore,
    InMemoryRevisionStore,
    ModalDictRevisionStore,
    Review,
    Revision,
    RevisionError,
    RevisionService,
    review_to_dict,
)
from control.workspace import (
    HEAD_SHA_MISMATCH,
    REVIEW_REQUIRED,
    InMemoryWorkspaceStore,
    WorkspaceService,
    WorkspaceSpec,
)

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git binary unavailable")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "sbx-test",
    "GIT_AUTHOR_EMAIL": "sbx-test@localhost",
    "GIT_COMMITTER_NAME": "sbx-test",
    "GIT_COMMITTER_EMAIL": "sbx-test@localhost",
}

SHA_A = "a" * 40
SHA_B = "b" * 40
GITHUB_REPO = "https://github.com/acme/widgets"


def host_git(cwd: Path, *args: str, raw: bool = False) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout if raw else res.stdout.strip()


def make_repo(root: Path, name: str = "origin") -> tuple[Path, str]:
    repo = root / name
    repo.mkdir()
    host_git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    host_git(repo, "add", "-A")
    host_git(repo, "commit", "-qm", "A")
    return repo, host_git(repo, "rev-parse", "HEAD")


def spec(repo: Path, base_sha: str) -> WorkspaceSpec:
    return WorkspaceSpec(repo=str(repo), base_ref="main", base_sha=base_sha)


def workdir_of(handle: SandboxHandle, workspaces: WorkspaceService, agent: str) -> Path:
    record = workspaces.get(agent)
    assert record is not None
    return Path(handle.root) / record.workdir


def write_in_workdir(
    handle: SandboxHandle,
    workspaces: WorkspaceService,
    agent: str,
    name: str = "b.txt",
    content: str = "two\n",
) -> Path:
    path = workdir_of(handle, workspaces, agent) / name
    path.write_text(content, encoding="utf-8")
    return path


def commit_all(workdir: Path, message: str = "B") -> str:
    host_git(workdir, "add", "-A")
    host_git(workdir, "commit", "-qm", message)
    return host_git(workdir, "rev-parse", "HEAD")


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
def revision_store() -> InMemoryRevisionStore:
    return InMemoryRevisionStore()


@pytest.fixture
def revisions(
    revision_store: InMemoryRevisionStore,
    artifacts: InMemoryArtifactStore,
    workspaces: WorkspaceService,
) -> RevisionService:
    # env={} keeps credential resolution hermetic — no ambient GH_TOKEN.
    return RevisionService(revision_store, artifacts, workspaces=workspaces, env={})


def _materialize(
    revisions: RevisionService,
    backend: LocalProcessBackend,
    handle: SandboxHandle,
    agent: str = "a1",
    run_n: int = 1,
    **kw: Any,
):
    return revisions.materialize(backend, handle, agent, run_id=f"run-{run_n}", run_n=run_n, **kw)


@needs_git
class TestMaterialize:
    def test_unchanged_run_produces_no_revision(
        self, tmp_path, backend, handle, workspaces, revisions, revision_store
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        assert _materialize(revisions, backend, handle) is None
        assert revision_store.list_revisions("a1") == []

    def test_dirty_worktree_materializes_patch_revision(
        self, tmp_path, backend, handle, workspaces, revisions, artifacts
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        write_in_workdir(handle, workspaces, "a1")
        revision = _materialize(revisions, backend, handle)
        assert revision is not None
        assert revision.status == "ready"
        assert revision.n == 1
        assert revision.run_id == "run-1"
        assert revision.base_sha == base
        assert revision.head_sha == base  # uncommitted: HEAD unmoved
        assert revision.artifact_id
        manifest = artifacts.manifest(revision.artifact_id)
        assert "patch.diff" in manifest.payloads
        assert revision.error is None

    def test_committed_run_materializes_bundle_revision(
        self, tmp_path, backend, handle, workspaces, revisions, artifacts
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        write_in_workdir(handle, workspaces, "a1")
        head = commit_all(workdir_of(handle, workspaces, "a1"))
        revision = _materialize(revisions, backend, handle)
        assert revision is not None
        assert revision.head_sha == head
        assert revision.base_sha == base
        manifest = artifacts.manifest(revision.artifact_id)
        assert "repo.bundle" in manifest.payloads

    def test_secret_in_change_fails_closed_and_is_durable(
        self, tmp_path, backend, handle, workspaces, revisions, revision_store
    ) -> None:
        """A forbidden value in the change → materialization_failed revision,
        durable after teardown — never silently dropped."""
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        write_in_workdir(handle, workspaces, "a1", content="token=SECRETVAL\n")
        revision = _materialize(revisions, backend, handle, forbidden_values=("SECRETVAL",))
        assert revision is not None
        assert revision.status == "materialization_failed"
        assert revision.artifact_id is None
        assert revision.error and "code" in revision.error
        assert revision_store.get_revision(revision.revision_id) is not None

    def test_new_revision_marks_prior_reviews_stale(
        self, tmp_path, backend, handle, workspaces, revisions
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        write_in_workdir(handle, workspaces, "a1", name="one.txt")
        rev1 = _materialize(revisions, backend, handle, run_n=1)
        review = revisions.add_review(rev1, reviewer_identity="key:test", verdict="approve")
        assert review.stale is False
        write_in_workdir(handle, workspaces, "a1", name="two.txt")
        rev2 = _materialize(revisions, backend, handle, run_n=2)
        assert rev2 is not None and rev2.n == 2
        stale = revisions.reviews(revision_id=rev1.revision_id)[0]
        assert stale.stale is True

    def test_review_on_non_latest_revision_is_born_stale(
        self, tmp_path, backend, handle, workspaces, revisions
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        write_in_workdir(handle, workspaces, "a1", name="one.txt")
        rev1 = _materialize(revisions, backend, handle, run_n=1)
        write_in_workdir(handle, workspaces, "a1", name="two.txt")
        rev2 = _materialize(revisions, backend, handle, run_n=2)
        review = revisions.add_review(rev1, reviewer_identity="key:test", verdict="approve")
        assert review.stale is True
        review2 = revisions.add_review(rev2, reviewer_identity="key:test", verdict="approve")
        assert review2.stale is False

    def test_file_store_survives_reopen(self, tmp_path: Path) -> None:
        """Teardown/recovery: a fresh service over the same dir sees the rows."""
        store1 = FileRevisionStore(tmp_path / "revs")
        svc1 = RevisionService(store1, InMemoryArtifactStore(), env={})
        svc1._store.put_revision(
            Revision(
                revision_id="rev-deadbeef01",
                agent_id="a1",
                n=1,
                head_sha=SHA_A,
                created_at="t",
                updated_at="t",
            )
        )
        svc2 = RevisionService(
            FileRevisionStore(tmp_path / "revs"), InMemoryArtifactStore(), env={}
        )
        rev = svc2.get("rev-deadbeef01")
        assert rev.head_sha == SHA_A
        assert svc2.latest("a1").revision_id == "rev-deadbeef01"


@needs_git
class TestDeliver:
    def test_deliver_after_sandbox_teardown(
        self, tmp_path, backend, handle, workspaces, revisions
    ) -> None:
        """Delivery operates on the durable revision — the author sandbox is
        terminated before delivery runs."""
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        write_in_workdir(handle, workspaces, "a1")
        head = commit_all(workdir_of(handle, workspaces, "a1"))
        revision = _materialize(revisions, backend, handle)
        backend.terminate(handle)  # author sandbox gone
        out = revisions.deliver(revision, overrides={"branch": "feat/x"})
        assert out.delivery["status"] == "delivered"
        assert out.delivery["pushed_head_sha"] == head
        remote_sha = subprocess.run(
            ["git", "ls-remote", str(origin), "refs/heads/feat/x"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()[0]
        assert remote_sha == head
        # mirrored onto the workspace record
        record = workspaces.get("a1")
        assert record.pushed_head_sha == head
        assert record.publish_error is None

    def test_deliver_patch_for_uncommitted_changes(
        self, tmp_path, backend, handle, workspaces, revisions
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        write_in_workdir(handle, workspaces, "a1")
        revision = _materialize(revisions, backend, handle)
        backend.terminate(handle)
        out = revisions.deliver(revision, overrides={"branch": "feat/patch"})
        pushed = out.delivery["pushed_head_sha"]
        assert pushed != base  # patch leg commits the working delta
        res = subprocess.run(
            ["git", "clone", "-q", "-b", "feat/patch", str(origin), str(tmp_path / "clone")],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        assert (tmp_path / "clone" / "b.txt").read_text() == "two\n"

    def test_delivery_failure_is_first_class(
        self, tmp_path, backend, handle, workspaces, revisions, revision_store
    ) -> None:
        """A failed push is durable delivery state + a raised error — not
        only ``workspace.publish_error``."""
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        write_in_workdir(handle, workspaces, "a1")
        revision = _materialize(revisions, backend, handle)
        backend.terminate(handle)
        revision.repo = str(tmp_path / "missing-repo")  # unreachable remote
        revision_store.put_revision(revision)
        with pytest.raises(RevisionError) as excinfo:
            revisions.deliver(revision)
        assert excinfo.value.code in ("repo_unavailable", "checkout_failed")
        stored = revision_store.get_revision(revision.revision_id)
        assert stored.delivery["status"] == "failed"
        assert stored.delivery["error"]["code"]
        record = workspaces.get("a1")
        assert record.publish_error  # legacy mirror only

    def test_not_ready_revision_refuses_delivery(self, revisions, revision_store) -> None:
        revision_store.put_revision(
            Revision(
                revision_id="rev-deadbeef02",
                agent_id="a1",
                n=1,
                status="materialization_failed",
                error={"code": "artifact_invalid", "message": "x"},
                created_at="t",
                updated_at="t",
            )
        )
        rev = revisions.get("rev-deadbeef02")
        with pytest.raises(RevisionError) as excinfo:
            revisions.deliver(rev)
        assert excinfo.value.code == REVISION_NOT_READY

    def test_sync_delivery_mirrors_publish_outcome(
        self, tmp_path, backend, handle, workspaces, revisions
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        write_in_workdir(handle, workspaces, "a1")
        revision = _materialize(revisions, backend, handle)
        record = workspaces.get("a1")
        record.publish_error = "repo_unavailable: boom"
        workspaces.save(record)
        out = revisions.sync_delivery(revision, record)
        assert out.delivery["status"] == "failed"
        assert out.delivery["error"]["code"] == "repo_unavailable"

    def test_deliver_replay_returns_recorded_delivery(
        self, tmp_path, backend, handle, workspaces, revisions
    ) -> None:
        """A retried deliver of an already-delivered revision returns the
        durable record — a patch-leg re-commit would mint a fresh sha and
        non-FF fail against its own remote head."""
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        write_in_workdir(handle, workspaces, "a1")
        revision = _materialize(revisions, backend, handle)
        backend.terminate(handle)
        out = revisions.deliver(revision, overrides={"branch": "feat/dup"})
        assert out.delivery["status"] == "delivered"
        pushed = out.delivery["pushed_head_sha"]

        again = revisions.deliver(out, overrides={"branch": "feat/dup"})
        assert again.delivery["status"] == "delivered"
        assert again.delivery["pushed_head_sha"] == pushed
        assert again.delivery["delivered_at"] == out.delivery["delivered_at"]

    def test_deliver_patch_retry_after_lost_record_converges(
        self, tmp_path, backend, handle, workspaces, revisions
    ) -> None:
        """Crash window: the push landed but the durable delivery record
        was lost. The pinned committer/author date re-mints the identical
        patch commit, so the retry's push is 'up-to-date' instead of a
        non-fast-forward rejection."""
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        write_in_workdir(handle, workspaces, "a1")
        revision = _materialize(revisions, backend, handle)
        backend.terminate(handle)
        out = revisions.deliver(revision, overrides={"branch": "feat/retry"})
        pushed = out.delivery["pushed_head_sha"]

        revision.delivery = None  # simulate the lost record
        retry = revisions.deliver(revision, overrides={"branch": "feat/retry"})
        assert retry.delivery["status"] == "delivered"
        assert retry.delivery["pushed_head_sha"] == pushed


class FakeRemote:
    """Test seam for the control-plane GitHub client."""

    def __init__(self, head_sha: str = SHA_B, state: str = "open") -> None:
        self.head_sha = head_sha
        self.state = state
        self.merge_calls: list[tuple[str, int, str | None]] = []
        self.comments: list[tuple[str, int, str]] = []
        self.get_pull_error: RemoteGitHubError | None = None

    def get_pull(self, slug: str, number: int) -> dict[str, Any]:
        if self.get_pull_error is not None:
            raise self.get_pull_error
        return {"head": {"sha": self.head_sha}, "state": self.state}

    def create_pull(self, slug: str, **kw: Any) -> dict[str, Any]:
        return {"number": 7, "html_url": f"https://github.com/{slug}/pull/7", "state": "open"}

    def merge_pull(self, slug: str, number: int, sha: str | None = None) -> dict[str, Any]:
        self.merge_calls.append((slug, number, sha))
        return {"merged": True, "sha": "e" * 40}

    def create_comment(self, slug: str, number: int, body: str) -> dict[str, Any]:
        self.comments.append((slug, number, body))
        return {"html_url": f"https://github.com/{slug}/issues/7#issuecomment-1"}


def _github_revision(
    revisions: RevisionService,
    revision_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    artifacts: InMemoryArtifactStore,
    pushed: str = SHA_B,
) -> Any:
    """A ready revision for a github.com repo with the push faked — the PR
    leg of the lifecycle without network access. The artifact is a real
    ``patch.diff`` package so ``deliver`` exercises ``_payload`` for real."""
    import hashlib

    from control.artifacts import ArtifactManifest

    monkeypatch.setattr("control.revisions.push_payload", lambda *a, **k: pushed)
    diff = b"diff --git a/b.txt b/b.txt\n+two\n"
    manifest = ArtifactManifest(
        artifact_id="art-x",
        base_sha=SHA_A,
        head_sha=SHA_B,
        repo=GITHUB_REPO,
        created_at="t",
        producer_agent_id="a1",
        payloads={"patch.diff": hashlib.sha256(diff).hexdigest()},
    )
    artifacts.put(manifest, {"patch.diff": diff})
    rev = Revision(
        revision_id="rev-cafef00d01",
        agent_id="a1",
        n=1,
        repo=GITHUB_REPO,
        base_sha=SHA_A,
        head_sha=SHA_B,
        artifact_id="art-x",
        created_at="t",
        updated_at="t",
    )
    revision_store.put_revision(rev)
    return rev


@needs_git
class TestReviewAndMerge:
    def test_review_records_identity_and_pin(
        self, revisions, revision_store, monkeypatch, artifacts
    ) -> None:
        rev = _github_revision(revisions, revision_store, monkeypatch, artifacts)
        review = revisions.add_review(
            rev,
            reviewer_identity="agent:a2",
            reviewer_agent_id="a2",
            reviewer_run_id="run-3",
            verdict="request_changes",
            findings=[{"message": "leaks a token", "path": "x.py"}],
        )
        assert review.reviewer_agent_id == "a2"
        assert review.reviewer_run_id == "run-3"
        assert review.reviewed_head_sha == SHA_B
        assert review.independent is True
        assert review.stale is False
        assert review.findings[0]["message"] == "leaks a token"

    def test_self_review_is_not_independent(
        self, revisions, revision_store, monkeypatch, artifacts
    ) -> None:
        rev = _github_revision(revisions, revision_store, monkeypatch, artifacts)
        by_agent = revisions.add_review(
            rev, reviewer_identity="agent:a1", reviewer_agent_id="a1", verdict="approve"
        )
        by_run = revisions.add_review(
            rev, reviewer_identity="agent:a2", reviewer_run_id="run-1", verdict="approve"
        )
        rev.run_id = "run-1"
        revision_store.put_revision(rev)
        by_run = revisions.add_review(
            rev, reviewer_identity="agent:a2", reviewer_run_id="run-1", verdict="approve"
        )
        assert by_agent.independent is False
        assert by_run.independent is False

    def test_merge_requires_delivered_pr(
        self, revisions, revision_store, monkeypatch, artifacts
    ) -> None:
        rev = _github_revision(revisions, revision_store, monkeypatch, artifacts)
        with pytest.raises(RevisionError) as excinfo:
            revisions.merge(rev)
        assert excinfo.value.code == DELIVERY_NOT_FOUND

    def _delivered(self, revisions, revision_store, monkeypatch, artifacts, remote) -> Any:
        rev = _github_revision(revisions, revision_store, monkeypatch, artifacts)
        revisions._remote = remote
        return revisions.deliver(rev, overrides={"pull_request": {"title": "t"}})

    def test_merge_requires_review(self, revisions, revision_store, monkeypatch, artifacts) -> None:
        remote = FakeRemote(head_sha=SHA_B)
        rev = self._delivered(revisions, revision_store, monkeypatch, artifacts, remote)
        with pytest.raises(RevisionError) as excinfo:
            revisions.merge(rev)
        assert excinfo.value.code == REVIEW_REQUIRED

    def test_merge_rejects_self_review(
        self, revisions, revision_store, monkeypatch, artifacts
    ) -> None:
        remote = FakeRemote(head_sha=SHA_B)
        rev = self._delivered(revisions, revision_store, monkeypatch, artifacts, remote)
        revisions.add_review(
            rev, reviewer_identity="agent:a1", reviewer_agent_id="a1", verdict="approve"
        )
        with pytest.raises(RevisionError) as excinfo:
            revisions.merge(rev)
        assert excinfo.value.code == INDEPENDENCE_VIOLATION

    def test_merge_rejects_non_approve(
        self, revisions, revision_store, monkeypatch, artifacts
    ) -> None:
        remote = FakeRemote(head_sha=SHA_B)
        rev = self._delivered(revisions, revision_store, monkeypatch, artifacts, remote)
        revisions.add_review(rev, reviewer_identity="key:k", verdict="request_changes")
        with pytest.raises(RevisionError) as excinfo:
            revisions.merge(rev)
        assert excinfo.value.code == REVIEW_REQUIRED

    def test_merge_rejects_stale_review(
        self, revisions, revision_store, monkeypatch, artifacts
    ) -> None:
        remote = FakeRemote(head_sha=SHA_B)
        rev = self._delivered(revisions, revision_store, monkeypatch, artifacts, remote)
        review = revisions.add_review(rev, reviewer_identity="key:k", verdict="approve")
        review.stale = True  # a newer revision landed
        revision_store.put_review(review)
        with pytest.raises(RevisionError) as excinfo:
            revisions.merge(rev)
        assert excinfo.value.code == REVIEW_REQUIRED

    def test_merge_rejects_remote_drift(
        self, revisions, revision_store, monkeypatch, artifacts
    ) -> None:
        """The remote PR head must still resolve to what was reviewed."""
        remote = FakeRemote(head_sha="f" * 40)  # remote moved on
        rev = self._delivered(revisions, revision_store, monkeypatch, artifacts, remote)
        revisions.add_review(rev, reviewer_identity="key:k", verdict="approve")
        with pytest.raises(RevisionError) as excinfo:
            revisions.merge(rev)
        assert excinfo.value.code == HEAD_SHA_MISMATCH
        assert remote.merge_calls == []

    def test_merge_pins_sha_server_side(
        self, revisions, revision_store, monkeypatch, artifacts
    ) -> None:
        remote = FakeRemote(head_sha=SHA_B)
        rev = self._delivered(revisions, revision_store, monkeypatch, artifacts, remote)
        revisions.add_review(rev, reviewer_identity="key:k", verdict="approve")
        out = revisions.merge(rev)
        assert out.delivery["merged"] is True
        assert out.delivery["merge_commit_sha"] == "e" * 40
        # the sha pin was sent — GitHub merges only if the PR head still is it
        assert remote.merge_calls == [("acme/widgets", 7, SHA_B)]

    def test_merge_ls_remote_fallback(
        self, revisions, revision_store, monkeypatch, artifacts
    ) -> None:
        """API unreachable → the same truth comes from ls-remote."""
        remote = FakeRemote(head_sha=SHA_B)
        remote.get_pull_error = RemoteGitHubError("github_unreachable", "down")
        rev = self._delivered(revisions, revision_store, monkeypatch, artifacts, remote)
        revisions.add_review(rev, reviewer_identity="key:k", verdict="approve")
        monkeypatch.setattr(
            "control.revisions.github_remote.ls_remote",
            lambda repo, ref, env=None: SHA_B,
        )
        out = revisions.merge(rev)
        assert out.delivery["merged"] is True

    def test_review_comment_posts_server_side(
        self, revisions, revision_store, monkeypatch, artifacts
    ) -> None:
        remote = FakeRemote(head_sha=SHA_B)
        rev = self._delivered(revisions, revision_store, monkeypatch, artifacts, remote)
        url = revisions.post_review_comment(rev, "sbx-review: approve")
        assert url.endswith("issuecomment-1")
        assert remote.comments == [("acme/widgets", 7, "sbx-review: approve")]

    def test_merge_replay_returns_durable_record(
        self, revisions, revision_store, monkeypatch, artifacts
    ) -> None:
        """A retried merge after success returns the durable record — the
        remote PR now reads merged, so re-running the drift check would
        fail closed on its own success."""
        remote = FakeRemote(head_sha=SHA_B)
        rev = self._delivered(revisions, revision_store, monkeypatch, artifacts, remote)
        revisions.add_review(rev, reviewer_identity="key:k", verdict="approve")
        out = revisions.merge(rev)
        assert out.delivery["merged"] is True
        remote.state = "merged"  # remote now reports the merge
        again = revisions.merge(out)
        assert again.delivery["merged"] is True
        assert again.delivery["merge_commit_sha"] == "e" * 40
        assert len(remote.merge_calls) == 1  # no second upstream merge


class _FakeDict:
    """``modal.Dict``-shaped fake: string keys, arbitrary values."""

    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.keys_calls = 0

    def get(self, key, default=None):
        return self.data.get(key, default)

    def put(self, key, value):
        self.data[key] = value

    def pop(self, key):
        return self.data.pop(key)

    def keys(self):
        self.keys_calls += 1
        return iter(list(self.data))


def _modal_store() -> tuple[ModalDictRevisionStore, _FakeDict]:
    """Dict-backed store without importing/connecting real modal."""
    store = ModalDictRevisionStore.__new__(ModalDictRevisionStore)
    store._dict = _FakeDict()
    store._index_ready = False
    return store, store._dict


def _review(review_id: str, revision_id: str, agent_id: str = "a1") -> Review:
    return Review(
        review_id=review_id,
        revision_id=revision_id,
        agent_id=agent_id,
        reviewer_identity="key:k",
        verdict="approve",
        reviewed_head_sha=SHA_B,
        created_at="t",
    )


class TestModalDictRevisionStoreReviews:
    """SOR-221 acceptance: ``current_review`` (the merge gate) calls
    ``list_reviews`` with only a ``revision_id`` — on the Dict backend that
    path used to read an ``__agents__`` index nothing ever wrote, so merge
    always failed ``review_required``."""

    def test_revision_scoped_lookup_finds_reviews(self) -> None:
        store, _ = _modal_store()
        store.put_review(_review("rvw-1", "rev-1"))
        store.put_review(_review("rvw-2", "rev-2", agent_id="a2"))
        rows = store.list_reviews(revision_id="rev-1")
        assert [r.review_id for r in rows] == ["rvw-1"]

    def test_current_review_satisfies_merge_gate(self, artifacts) -> None:
        store, _ = _modal_store()
        svc = RevisionService(store, artifacts, env={})
        rev = Revision(
            revision_id="rev-cafef00d02",
            agent_id="a1",
            n=1,
            repo=GITHUB_REPO,
            base_sha=SHA_A,
            head_sha=SHA_B,
            created_at="t",
            updated_at="t",
        )
        store.put_revision(rev)
        review = svc.add_review(rev, reviewer_identity="key:k", verdict="approve")
        current = svc.current_review(rev)
        assert current is not None and current.review_id == review.review_id

    def test_legacy_dict_backfills_agent_index_once(self) -> None:
        """Dicts written before ``__agents__`` existed rebuild it once from
        the ``reviews/`` per-agent index keys; the warm path stays indexed
        (no repeated enumeration)."""
        store, d = _modal_store()
        review = _review("rvw-9", "rev-1", agent_id="a9")
        d.put(f"review/{review.review_id}", review_to_dict(review))
        d.put(f"reviews/{review.agent_id}", ["rvw-9"])

        rows = store.list_reviews(revision_id="rev-1")
        assert [r.review_id for r in rows] == ["rvw-9"]
        assert d.get("__agents__") == ["a9"]
        calls = d.keys_calls
        assert store.list_reviews(revision_id="rev-1")[0].review_id == "rvw-9"
        assert d.keys_calls == calls
