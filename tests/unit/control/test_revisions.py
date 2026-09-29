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
import threading
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
    WorkspaceRecord,
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

    def test_same_run_materialize_replays_existing_revision(
        self, tmp_path, backend, handle, workspaces, revisions, revision_store
    ) -> None:
        """A re-entrant finish (reconcile settling the same turn from
        evidence) must replay the recorded revision — one run, one
        revision."""
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        write_in_workdir(handle, workspaces, "a1")
        rev1 = _materialize(revisions, backend, handle)
        assert rev1 is not None and rev1.status == "ready"
        again = _materialize(revisions, backend, handle)
        assert again is not None and again.revision_id == rev1.revision_id
        assert len(revision_store.list_revisions("a1")) == 1

    def test_concurrent_materialize_same_run_lands_one_row(
        self, tmp_path, backend, handle, workspaces, revisions, revision_store, monkeypatch
    ) -> None:
        """B7: settle + reconcile racing the same run's commit — the
        identity lock serializes to exactly one durable row."""
        import control.revisions as revisions_mod

        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        write_in_workdir(handle, workspaces, "a1")

        real_snapshot = revisions_mod.snapshot_workspace_artifact
        gate = threading.Barrier(2)

        def slow_snapshot(**kw: Any) -> Any:
            # Both callers must clear the unlocked fast-path check before
            # either commits — the commit lock alone decides the winner.
            gate.wait(timeout=15)
            return real_snapshot(**kw)

        monkeypatch.setattr(revisions_mod, "snapshot_workspace_artifact", slow_snapshot)

        results: list[Any] = []
        errors: list[BaseException] = []

        def go() -> None:
            try:
                results.append(_materialize(revisions, backend, handle))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert all(not t.is_alive() for t in threads)
        assert not errors and len(results) == 2
        rows = revision_store.list_revisions("a1")
        assert len(rows) == 1
        assert rows[0].status == "ready"
        assert {r.revision_id for r in results} == {rows[0].revision_id}

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


def _stored_revision(revision_id: str, run_id: str | None, n: int, **kw: Any) -> Revision:
    row = {
        "agent_id": "a1",
        "task_id": "t1",
        "repo": "https://github.com/acme/widgets",
        "base_sha": SHA_A,
        "head_sha": SHA_B,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    row.update(kw)
    return Revision(revision_id=revision_id, run_id=run_id, n=n, **row)


class TestReadModelDedupe:
    """B7: revision identity + ordering on the read side.

    Rows already persisted for one logical revision — a settle/reconcile
    race, or a failed materialization retried to ready — collapse to a
    single representative; ``list``/``latest``/``resolve`` agree and
    ordering stays deterministic even when duplicates share an ``n``.
    """

    def test_duplicate_run_rows_collapse(
        self, revision_store: InMemoryRevisionStore, revisions: RevisionService
    ) -> None:
        for i in range(5):
            revision_store.put_revision(
                _stored_revision(
                    f"rev-dup-{i}",
                    "run-1",
                    1,
                    created_at=f"2026-01-01T00:00:0{i}+00:00",
                    updated_at=f"2026-01-01T00:00:0{i}+00:00",
                )
            )
        rows = revisions.list("a1")
        assert len(rows) == 1
        assert rows[0].run_id == "run-1"
        assert revisions.resolve("a1", "1").revision_id == rows[0].revision_id
        assert revisions.latest("a1").revision_id == rows[0].revision_id
        # get() still fetches the hidden duplicates by id — history stays
        # auditable, only the listing collapses.
        assert revisions.get("rev-dup-4").revision_id == "rev-dup-4"

    def test_failed_then_ready_collapses_to_ready(
        self, revision_store: InMemoryRevisionStore, revisions: RevisionService
    ) -> None:
        revision_store.put_revision(
            _stored_revision(
                "rev-fail-1",
                "run-1",
                1,
                status="materialization_failed",
                error={"code": "artifact_invalid", "message": "boom"},
            )
        )
        revision_store.put_revision(_stored_revision("rev-ready-1", "run-1", 2))
        rows = revisions.list("a1")
        assert [r.revision_id for r in rows] == ["rev-ready-1"]
        assert rows[0].status == "ready"

    def test_delivery_survives_collapse(
        self, revision_store: InMemoryRevisionStore, revisions: RevisionService
    ) -> None:
        """Delivery state corresponds to its revision: the row carrying the
        delivered record wins over a later duplicate without one."""
        revision_store.put_revision(
            _stored_revision(
                "rev-deliv-1",
                "run-1",
                1,
                delivery={
                    "status": "delivered",
                    "branch": "sbx/a1",
                    "pushed_head_sha": SHA_B,
                    "delivered_at": "2026-01-01T00:00:05+00:00",
                },
                updated_at="2026-01-01T00:00:05+00:00",
            )
        )
        revision_store.put_revision(
            _stored_revision("rev-dup-1", "run-1", 1, updated_at="2026-01-01T00:00:09+00:00")
        )
        rows = revisions.list("a1")
        assert [r.revision_id for r in rows] == ["rev-deliv-1"]
        assert rows[0].delivery["status"] == "delivered"
        assert revisions.latest("a1").delivery["branch"] == "sbx/a1"

    def test_multi_run_ordering_is_deterministic(
        self, revision_store: InMemoryRevisionStore, revisions: RevisionService
    ) -> None:
        revision_store.put_revision(_stored_revision("rev-b", "run-2", 2))
        revision_store.put_revision(_stored_revision("rev-a1", "run-1", 1))
        revision_store.put_revision(_stored_revision("rev-a2", "run-1", 1))
        revision_store.put_revision(_stored_revision("rev-manual", None, 3))
        rows = revisions.list("a1")
        assert [r.n for r in rows] == [1, 2, 3]
        # Identical duplicates break deterministically on revision_id.
        assert rows[0].revision_id == max("rev-a1", "rev-a2")
        assert [r.revision_id for r in rows[1:]] == ["rev-b", "rev-manual"]

    def test_repeat_failure_replays_recorded_row(
        self, revision_store: InMemoryRevisionStore, revisions: RevisionService
    ) -> None:
        first = revisions._commit(
            "a1",
            run_id="run-1",
            task_id="t1",
            status="materialization_failed",
            error={"code": "artifact_invalid", "message": "one"},
        )
        again = revisions._commit(
            "a1",
            run_id="run-1",
            task_id="t1",
            status="materialization_failed",
            error={"code": "artifact_invalid", "message": "two"},
        )
        assert again.revision_id == first.revision_id
        assert len(revision_store.list_revisions("a1")) == 1


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

    def test_deliver_newer_revision_stacks_on_delivery_tip(
        self, tmp_path, backend, handle, workspaces, revisions
    ) -> None:
        """SOR-221: re-delivering a *newer* revision onto the already-
        delivered branch must not non-FF fail — the earlier delivery commit
        is host-minted, so the payload tree is re-anchored on top of the
        live branch tip and the push stays fast-forward."""
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        write_in_workdir(handle, workspaces, "a1")
        rev1 = _materialize(revisions, backend, handle)
        out1 = revisions.deliver(rev1, overrides={"branch": "feat/stack"})
        first = out1.delivery["pushed_head_sha"]

        write_in_workdir(handle, workspaces, "a1", name="c.txt", content="three\n")
        rev2 = _materialize(revisions, backend, handle, run_n=2)
        out2 = revisions.deliver(rev2, overrides={"branch": "feat/stack"})
        second = out2.delivery["pushed_head_sha"]

        assert second != first
        remote_sha = subprocess.run(
            ["git", "ls-remote", str(origin), "refs/heads/feat/stack"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()[0]
        assert remote_sha == second
        # The second delivery descends from the first — a fast-forward.
        ancestry = subprocess.run(
            ["git", "-C", str(origin), "merge-base", "--is-ancestor", first, second],
            capture_output=True,
        )
        assert ancestry.returncode == 0
        # And the branch carries the full cumulative revision content —
        # no stray payload file leaks into the delivery commit.
        res = subprocess.run(
            ["git", "clone", "-q", "-b", "feat/stack", str(origin), str(tmp_path / "clone2")],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        clone = tmp_path / "clone2"
        assert (clone / "a.txt").read_text() == "one\n"
        assert (clone / "b.txt").read_text() == "two\n"
        assert (clone / "c.txt").read_text() == "three\n"
        assert not (clone / "payload.diff").exists()


class FakeRemote:
    """Test seam for the control-plane GitHub client."""

    def __init__(
        self,
        head_sha: str = SHA_B,
        state: str = "open",
        *,
        draft: bool = False,
        head_ref: str = "sbx/a1",
    ) -> None:
        self.head_sha = head_sha
        self.state = state
        self.draft = draft
        self.head_ref = head_ref
        self.merge_calls: list[tuple[str, int, str | None]] = []
        self.comments: list[tuple[str, int, str]] = []
        self.create_calls: list[dict[str, Any]] = []
        self.find_calls: list[tuple[str, str]] = []
        self.find_results: dict[str, dict[str, Any] | None] = {}
        self.update_calls: list[dict[str, Any]] = []
        self.update_error: RemoteGitHubError | None = None
        self.update_result: dict[str, Any] | None = None
        self.get_pull_error: RemoteGitHubError | None = None
        self.merge_error: RemoteGitHubError | None = None

    def get_pull(self, slug: str, number: int) -> dict[str, Any]:
        if self.get_pull_error is not None:
            raise self.get_pull_error
        return {
            "number": number,
            "head": {"sha": self.head_sha, "ref": self.head_ref},
            "state": self.state,
            "draft": self.draft,
        }

    def find_pull(self, slug: str, head_branch: str) -> dict[str, Any] | None:
        self.find_calls.append((slug, head_branch))
        return self.find_results.get(head_branch)

    def update_pull(self, slug: str, number: int, **kw: Any) -> dict[str, Any]:
        self.update_calls.append(dict(kw))
        if self.update_error is not None:
            raise self.update_error
        if self.update_result is not None:
            return self.update_result
        return {
            "number": number,
            "html_url": f"https://github.com/{slug}/pull/{number}",
            "state": "open",
            "draft": bool(kw.get("draft")),
            "base": {"ref": kw.get("base") or "main"},
        }

    def create_pull(self, slug: str, **kw: Any) -> dict[str, Any]:
        self.create_calls.append(dict(kw))
        return {
            "number": 7,
            "html_url": f"https://github.com/{slug}/pull/7",
            "state": "open",
            "draft": bool(kw.get("draft")),
        }

    def merge_pull(self, slug: str, number: int, sha: str | None = None) -> dict[str, Any]:
        self.merge_calls.append((slug, number, sha))
        if self.merge_error is not None:
            raise self.merge_error
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

    def test_review_idempotency_pin_roundtrips(
        self, revisions, revision_store, monkeypatch, artifacts
    ) -> None:
        rev = _github_revision(revisions, revision_store, monkeypatch, artifacts)
        pin = {"key_id": "key-1", "key": "review:a1:idem-1", "fingerprint": "fp"}
        review = revisions.add_review(
            rev, reviewer_identity="key:k", verdict="approve", idempotency=pin
        )
        assert review.idempotency == pin
        # The pin survives the durable serialization round-trip, so a replay
        # landing after a control-plane restart still resolves the review.
        reloaded = revision_store.get_review(review.review_id)
        assert reloaded is not None and reloaded.idempotency == pin
        found = revisions.find_review_by_idempotency("a1", "key-1", "review:a1:idem-1")
        assert found is not None and found.review_id == review.review_id
        assert revisions.find_review_by_idempotency("a1", "key-1", "review:a1:other") is None
        assert revisions.find_review_by_idempotency("a1", "key-2", "review:a1:idem-1") is None

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

    def test_merge_draft_pr_is_structured_merge_not_allowed(
        self, revisions, revision_store, monkeypatch, artifacts
    ) -> None:
        """A draft PR is not mergeable — the refusal is a canonical 409
        ``merge_not_allowed``, never an unstructured 500."""
        remote = FakeRemote(head_sha=SHA_B, draft=True)
        rev = self._delivered(revisions, revision_store, monkeypatch, artifacts, remote)
        revisions.add_review(rev, reviewer_identity="key:k", verdict="approve")
        with pytest.raises(RevisionError) as excinfo:
            revisions.merge(rev)
        assert excinfo.value.code == "merge_not_allowed"
        assert excinfo.value.status_code == 409
        assert remote.merge_calls == []

    def test_merge_pull_405_maps_to_merge_not_allowed(
        self, revisions, revision_store, monkeypatch, artifacts
    ) -> None:
        """When get_pull missed the draft flag, GitHub's 405 refusal maps
        to the same structured code instead of leaking as repo_unavailable."""
        remote = FakeRemote(head_sha=SHA_B)
        remote.merge_error = RemoteGitHubError(
            "repo_unavailable", "GitHub PUT merge failed (http 405): draft", status=405
        )
        rev = self._delivered(revisions, revision_store, monkeypatch, artifacts, remote)
        revisions.add_review(rev, reviewer_identity="key:k", verdict="approve")
        with pytest.raises(RevisionError) as excinfo:
            revisions.merge(rev)
        assert excinfo.value.code == "merge_not_allowed"
        assert excinfo.value.status_code == 409

    def test_merge_pull_409_maps_to_head_sha_mismatch(
        self, revisions, revision_store, monkeypatch, artifacts
    ) -> None:
        remote = FakeRemote(head_sha=SHA_B)
        remote.merge_error = RemoteGitHubError(
            "repo_unavailable", "GitHub PUT merge failed (http 409): head moved", status=409
        )
        rev = self._delivered(revisions, revision_store, monkeypatch, artifacts, remote)
        revisions.add_review(rev, reviewer_identity="key:k", verdict="approve")
        with pytest.raises(RevisionError) as excinfo:
            revisions.merge(rev)
        assert excinfo.value.code == HEAD_SHA_MISMATCH

    def test_merge_pull_transport_error_stays_repo_unavailable(
        self, revisions, revision_store, monkeypatch, artifacts
    ) -> None:
        remote = FakeRemote(head_sha=SHA_B)
        remote.merge_error = RemoteGitHubError(
            "repo_unavailable", "GitHub PUT merge failed (http 502)", status=502
        )
        rev = self._delivered(revisions, revision_store, monkeypatch, artifacts, remote)
        revisions.add_review(rev, reviewer_identity="key:k", verdict="approve")
        with pytest.raises(RevisionError) as excinfo:
            revisions.merge(rev)
        assert excinfo.value.code == "repo_unavailable"
        assert excinfo.value.status_code == 502


@needs_git
class TestDeliverPullRequestBranch:
    """SOR-221: a recorded PR is only valid for the branch it tracks.

    Delivering a revision to a *different* branch must find-or-create the
    PR for the pushed head — carrying the old PR's number forward makes
    ``merge`` drift-check the wrong pull request (the acceptance saw
    ``head_sha_mismatch`` on a mis-linked PR #28).
    """

    def _ws_with_pr(self, workspaces: WorkspaceService, pr_state: str = "open") -> None:
        workspaces.save(
            WorkspaceRecord(
                agent_id="a1",
                repo=GITHUB_REPO,
                base_ref="main",
                base_sha=SHA_A,
                branch="sbx/a1",
                pushed_head_sha="0" * 40,
                pull_request={
                    "number": 28,
                    "url": "https://github.com/acme/widgets/pull/28",
                    "state": pr_state,
                    "ref": "refs/pull/28/head",
                    "head_sha": "0" * 40,
                    "head_branch": "sbx/a1",
                    "base": "main",
                },
            )
        )

    def test_deliver_reuses_pr_tracking_the_same_branch(
        self, revisions, revision_store, monkeypatch, artifacts, workspaces
    ) -> None:
        self._ws_with_pr(workspaces)
        remote = FakeRemote(head_sha=SHA_B, head_ref="sbx/a1")
        revisions._remote = remote
        rev = _github_revision(revisions, revision_store, monkeypatch, artifacts)
        out = revisions.deliver(rev, overrides={"pull_request": {"title": "t"}})
        pr = out.delivery["pull_request"]
        assert pr["number"] == 28  # same open PR updated, not recreated
        assert pr["head_sha"] == SHA_B
        assert pr["head_branch"] == "sbx/a1"
        assert remote.create_calls == []
        # The title override was applied upstream, not dropped.
        assert remote.update_calls == [{"title": "t", "body": None, "base": None, "draft": None}]

    def test_deliver_other_branch_finds_matching_pr(
        self, revisions, revision_store, monkeypatch, artifacts, workspaces
    ) -> None:
        self._ws_with_pr(workspaces)
        remote = FakeRemote(head_sha=SHA_B, head_ref="sbx/a1")
        remote.find_results["sbx/alt"] = {
            "number": 30,
            "html_url": "https://github.com/acme/widgets/pull/30",
            "state": "open",
            "draft": False,
        }
        revisions._remote = remote
        rev = _github_revision(revisions, revision_store, monkeypatch, artifacts)
        out = revisions.deliver(
            rev, overrides={"branch": "sbx/alt", "pull_request": {"title": "t"}}
        )
        pr = out.delivery["pull_request"]
        assert pr["number"] == 30  # the PR for the pushed branch, not #28
        assert pr["head_branch"] == "sbx/alt"
        assert remote.find_calls == [("acme/widgets", "sbx/alt")]
        assert remote.create_calls == []

    def test_deliver_other_branch_creates_pr_when_none_exists(
        self, revisions, revision_store, monkeypatch, artifacts, workspaces
    ) -> None:
        self._ws_with_pr(workspaces)
        remote = FakeRemote(head_sha=SHA_B, head_ref="sbx/a1")
        revisions._remote = remote
        rev = _github_revision(revisions, revision_store, monkeypatch, artifacts)
        out = revisions.deliver(
            rev, overrides={"branch": "sbx/alt", "pull_request": {"title": "t"}}
        )
        pr = out.delivery["pull_request"]
        assert pr["number"] == 7  # freshly created for sbx/alt
        assert pr["head_branch"] == "sbx/alt"
        assert remote.create_calls == [
            {"head": "sbx/alt", "base": "main", "title": "t", "body": "", "draft": False}
        ]
        # The workspace record now tracks the latest delivery's PR.
        record = workspaces.get("a1")
        assert record.pull_request["number"] == 7
        assert record.branch == "sbx/alt"

    @pytest.mark.parametrize("terminal_state", ["merged", "closed"])
    def test_deliver_new_revision_reanchors_terminal_workspace_pr(
        self, terminal_state, revisions, revision_store, monkeypatch, artifacts, workspaces
    ) -> None:
        """SOR-221 recovery: ``merge`` leaves ``record.pull_request`` in a
        terminal state, so the NEXT revision's deliver must never reuse it —
        the find-or-create leg re-anchors a current PR and head."""
        self._ws_with_pr(workspaces, pr_state=terminal_state)
        remote = FakeRemote(head_sha=SHA_B, head_ref="sbx/a1")
        revisions._remote = remote
        rev = _github_revision(revisions, revision_store, monkeypatch, artifacts)
        rev.n = 2
        out = revisions.deliver(rev, overrides={"pull_request": {"title": "t"}})
        pr = out.delivery["pull_request"]
        assert pr["number"] == 7  # fresh PR — terminal #28 was not reused
        assert pr["state"] == "open"
        assert pr["head_sha"] == SHA_B  # the current push, not the stale recorded head
        assert pr["head_branch"] == "sbx/a1"
        # Find-or-create ran for the pushed branch; #28 was never patched.
        assert remote.find_calls == [("acme/widgets", "sbx/a1")]
        assert remote.create_calls == [
            {"head": "sbx/a1", "base": "main", "title": "t", "body": "", "draft": False}
        ]
        assert remote.update_calls == []
        # The durable delivery and the workspace record track the fresh PR.
        stored = revision_store.get_revision(out.revision_id)
        assert stored.delivery["pull_request"]["number"] == 7
        assert workspaces.get("a1").pull_request["number"] == 7

    def test_deliver_new_revision_reanchors_pr_terminal_upstream(
        self, revisions, revision_store, monkeypatch, artifacts, workspaces
    ) -> None:
        """The recorded state can lag upstream: a recorded-open PR that is
        closed live is likewise dropped and re-anchored, not reused."""
        self._ws_with_pr(workspaces, pr_state="open")
        remote = FakeRemote(head_sha=SHA_B, head_ref="sbx/a1", state="closed")
        revisions._remote = remote
        rev = _github_revision(revisions, revision_store, monkeypatch, artifacts)
        rev.n = 2
        out = revisions.deliver(rev, overrides={"pull_request": {"title": "t"}})
        pr = out.delivery["pull_request"]
        assert pr["number"] == 7  # live-terminal #28 was not reused
        assert pr["state"] == "open"
        assert pr["head_sha"] == SHA_B
        assert remote.create_calls == [
            {"head": "sbx/a1", "base": "main", "title": "t", "body": "", "draft": False}
        ]
        assert remote.update_calls == []

    def test_deliver_replay_honors_new_pull_request_override(
        self, revisions, revision_store, monkeypatch, artifacts, workspaces
    ) -> None:
        """SOR-221: the idempotent replay only covers work the recorded
        delivery already did — re-delivering with a ``pull_request``
        override on a push-only delivery must run the PR leg, not no-op."""
        remote = FakeRemote(head_sha=SHA_B, head_ref="sbx/a1")
        revisions._remote = remote
        rev = _github_revision(revisions, revision_store, monkeypatch, artifacts)
        out = revisions.deliver(rev, overrides={"branch": "sbx/a1"})
        assert out.delivery["status"] == "delivered"
        assert out.delivery["pull_request"] is None

        again = revisions.deliver(out, overrides={"pull_request": {"title": "t"}})
        pr = again.delivery["pull_request"]
        assert pr["number"] == 7
        assert pr["head_branch"] == "sbx/a1"
        assert remote.create_calls == [
            {"head": "sbx/a1", "base": "main", "title": "t", "body": "", "draft": False}
        ]

    def test_deliver_replay_after_pr_went_terminal_opens_fresh(
        self, revisions, revision_store, monkeypatch, artifacts, workspaces
    ) -> None:
        """A replay whose recorded PR closed upstream is not a replay —
        the PR leg re-runs and opens a fresh request."""
        remote = FakeRemote(head_sha=SHA_B, head_ref="sbx/a1")
        revisions._remote = remote
        rev = _github_revision(revisions, revision_store, monkeypatch, artifacts)
        out = revisions.deliver(rev, overrides={"branch": "sbx/a1", "pull_request": {"title": "t"}})
        assert out.delivery["pull_request"]["number"] == 7
        # The recorded PR went terminal upstream.
        delivery = dict(out.delivery)
        delivery["pull_request"] = {**delivery["pull_request"], "state": "closed"}
        out.delivery = delivery

        again = revisions.deliver(out, overrides={"pull_request": {"title": "t2"}})
        assert again.delivery["pull_request"]["number"] == 7
        assert len(remote.create_calls) == 2  # a fresh PR was opened

    def test_deliver_replay_applies_overrides_on_recorded_open_pr(
        self, revisions, revision_store, monkeypatch, artifacts, workspaces
    ) -> None:
        """The idempotent push replay is not a blanket no-op: ``deliver``
        is "open/update the PR", so a replay carrying pull_request fields
        PATCHes them onto the recorded open request and persists them."""
        self._ws_with_pr(workspaces)
        remote = FakeRemote(head_sha=SHA_B, head_ref="sbx/a1")
        revisions._remote = remote
        rev = _github_revision(revisions, revision_store, monkeypatch, artifacts)
        out = revisions.deliver(rev, overrides={"pull_request": {"title": "t"}})
        assert out.delivery["status"] == "delivered"
        assert out.delivery["pull_request"]["number"] == 28
        remote.update_calls.clear()

        again = revisions.deliver(
            out, overrides={"pull_request": {"target": "release", "draft": False}}
        )
        assert remote.update_calls == [
            {"title": None, "body": None, "base": "release", "draft": False}
        ]
        pr = again.delivery["pull_request"]
        assert pr["number"] == 28
        assert pr["base"] == "release"
        assert pr["draft"] is False
        # The durable record holds the update, so a later merge gates on it.
        stored = revision_store.get_revision(again.revision_id)
        assert stored.delivery["pull_request"]["base"] == "release"

    def test_deliver_replay_override_failure_surfaces_without_breaking_delivery(
        self, revisions, revision_store, monkeypatch, artifacts, workspaces
    ) -> None:
        """An upstream refusal on a replay override is a structured error —
        the already-delivered record stays delivered (the push+PR stand)."""
        self._ws_with_pr(workspaces)
        remote = FakeRemote(head_sha=SHA_B, head_ref="sbx/a1")
        revisions._remote = remote
        rev = _github_revision(revisions, revision_store, monkeypatch, artifacts)
        out = revisions.deliver(rev, overrides={"pull_request": {"title": "t"}})
        assert out.delivery["status"] == "delivered"

        remote.update_error = RemoteGitHubError("repo_unavailable", "http 403: no scope")
        with pytest.raises(RevisionError) as exc:
            revisions.deliver(out, overrides={"pull_request": {"title": "nope"}})
        assert exc.value.code == "repo_unavailable"
        assert out.delivery["status"] == "delivered"

    def test_void_for_run_demotes_ready_revision(self, revisions, revision_store) -> None:
        """A run cancelled after materialization must not keep a
        deliverable revision — void demotes ready rows for that run only."""
        revision_store.put_revision(
            Revision(
                revision_id="rev-voided",
                agent_id="a1",
                n=1,
                run_id="run-1",
                head_sha=SHA_B,
                created_at="t",
                updated_at="t",
            )
        )
        revision_store.put_revision(
            Revision(
                revision_id="rev-keeper",
                agent_id="a1",
                n=2,
                run_id="run-2",
                head_sha=SHA_B,
                created_at="t",
                updated_at="t",
            )
        )
        assert revisions.void_for_run("a1", "run-1", code="run_cancelled", message="m") == 1
        voided = revision_store.get_revision("rev-voided")
        assert voided.status == "materialization_failed"
        assert voided.error["code"] == "run_cancelled"
        assert revision_store.get_revision("rev-keeper").status == "ready"
        # Already-voided / other runs are untouched.
        assert revisions.void_for_run("a1", "run-1", code="run_cancelled", message="m") == 0


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
