"""SOR-128 git policy: validation, durable metadata, publish, PR-ref handoff.

Real-local-git seams — ``LocalProcessBackend`` + on-disk repos/file remotes —
so branch materialization, push verification and PR-ref fetch run the same
git invocations production does. GitHub REST (PR create / review comment)
is a monkeypatched seam: no token, no network, no shared-identity risk.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from control.backend import LocalProcessBackend, SandboxHandle, SandboxSpec
from control.handoff import HandoffService, InMemoryArtifactStore
from control.workspace import (
    BASE_SHA_MISMATCH,
    HEAD_SHA_MISMATCH,
    REPO_UNAVAILABLE,
    REVIEW_REQUIRED,
    WORKSPACE_INVALID,
    WORKSPACE_NOT_FOUND,
    InMemoryWorkspaceStore,
    WorkspaceError,
    WorkspaceRecord,
    WorkspaceService,
    WorkspaceSpec,
    is_safe_ref,
    normalize_git_policy,
    record_from_dict,
    record_to_dict,
    validate_git_policy,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "sbx-test",
    "GIT_AUTHOR_EMAIL": "sbx-test@localhost",
    "GIT_COMMITTER_NAME": "sbx-test",
    "GIT_COMMITTER_EMAIL": "sbx-test@localhost",
}

SHA_A = "a" * 40
SHA_B = "b" * 40
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


def commit_file(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    host_git(repo, "add", "-A")
    host_git(repo, "commit", "-qm", f"add {name}")
    return host_git(repo, "rev-parse", "HEAD")


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
def handoff(workspaces: WorkspaceService) -> HandoffService:
    return HandoffService(workspaces, InMemoryArtifactStore())


def spec(repo: Path, base_sha: str) -> WorkspaceSpec:
    return WorkspaceSpec(repo=str(repo), base_ref="main", base_sha=base_sha)


class TestSafeRef:
    @pytest.mark.parametrize(
        "ref",
        ["main", "feat/x", "sbx/agent-1", "refs/pull/7/head", "pull/7/head", "v1.2.3", "a-b_c.d"],
    )
    def test_safe(self, ref: str) -> None:
        assert is_safe_ref(ref)

    @pytest.mark.parametrize(
        "ref",
        [
            "",
            "-x",
            "--upload-pack=x",
            "/x",
            "x/",
            "x.",
            ".x",
            "@",
            "a..b",
            "a b",
            "a;b",
            "a$b",
            "a'b",
            "a`b",
            "a~b",
            "a:b",
            "a?b",
            "a*b",
            "a[b",
            "a\\b",
            "a@{b}",
            "a//b",
            "a/x.lock",
            "../x",
            "a/../b",
            None,
            7,
        ],
    )
    def test_unsafe(self, ref: Any) -> None:
        assert not is_safe_ref(ref)


class TestPolicyValidation:
    def test_valid_combinations(self) -> None:
        validate_git_policy({"branch": "feat/x", "push": True})
        validate_git_policy({"push": True, "auto_create_pr": True, "draft": True})
        validate_git_policy({"branch": "b", "target": "main", "title": "t", "body": "x"})
        validate_git_policy({})

    def test_auto_create_pr_requires_push(self) -> None:
        with pytest.raises(WorkspaceError) as exc:
            validate_git_policy({"auto_create_pr": True})
        assert exc.value.code == WORKSPACE_INVALID

    def test_auto_publish_requires_push(self) -> None:
        with pytest.raises(WorkspaceError) as exc:
            validate_git_policy({"auto_publish": True})
        assert exc.value.code == WORKSPACE_INVALID
        validate_git_policy({"push": True, "auto_publish": True})

    def test_merge_requires_auto_create_pr(self) -> None:
        with pytest.raises(WorkspaceError) as exc:
            validate_git_policy({"push": True, "merge": True})
        assert exc.value.code == WORKSPACE_INVALID
        validate_git_policy({"push": True, "auto_create_pr": True, "merge": True})

    @pytest.mark.parametrize("key", ["branch", "target"])
    def test_unsafe_ref_names(self, key: str) -> None:
        for bad in ("-x", "a..b", "a b", "a;b", "/x"):
            with pytest.raises(WorkspaceError) as exc:
                validate_git_policy({key: bad})
            assert exc.value.code == WORKSPACE_INVALID

    def test_unknown_keys(self) -> None:
        with pytest.raises(WorkspaceError) as exc:
            validate_git_policy({"bogus": 1})
        assert exc.value.code == WORKSPACE_INVALID

    def test_non_dict(self) -> None:
        with pytest.raises(WorkspaceError) as exc:
            validate_git_policy("push")
        assert exc.value.code == WORKSPACE_INVALID

    def test_normalize_defaults(self) -> None:
        out = normalize_git_policy({"push": True}, agent_id="agent-xyz", base_ref="main")
        assert out is not None
        assert out["branch"] == "sbx/agent-xyz"
        assert out["target"] == "main"
        assert out["push"] is True
        assert out["auto_create_pr"] is False
        assert out["auto_publish"] is False
        assert out["merge"] is False

    def test_normalize_explicit(self) -> None:
        out = normalize_git_policy(
            {"branch": "feat/x", "target": "release", "push": True, "draft": True},
            agent_id="a1",
            base_ref="main",
        )
        assert out is not None
        assert out["branch"] == "feat/x"
        assert out["target"] == "release"
        assert out["draft"] is True

    def test_normalize_none(self) -> None:
        assert normalize_git_policy(None, agent_id="a1", base_ref="main") is None

    def test_normalize_validates(self) -> None:
        with pytest.raises(WorkspaceError):
            normalize_git_policy({"auto_create_pr": True}, agent_id="a1", base_ref="main")


class TestRecordCodec:
    def _record(self) -> WorkspaceRecord:
        return WorkspaceRecord(
            agent_id="a1",
            repo="/r",
            base_ref="main",
            base_sha=SHA_A,
            checkout_sha=SHA_A,
            head_sha=SHA_B,
            git={
                "branch": "sbx/a1",
                "push": True,
                "auto_create_pr": True,
                "auto_publish": True,
                "merge": True,
                "target": "main",
                "draft": False,
                "title": "t",
                "body": None,
            },
            branch="sbx/a1",
            pushed_head_sha=SHA_B,
            pull_request={
                "number": 7,
                "url": "https://example.test/pr/7",
                "state": "open",
                "ref": "refs/pull/7/head",
                "head_sha": SHA_B,
                "base": "main",
                "draft": False,
                "review_comment_url": "https://example.test/c/1",
            },
            merge={
                "merged": True,
                "merge_commit_sha": SHA_0,
                "head_sha": SHA_B,
                "merged_at": "t2",
            },
            publish_error="repo_unavailable: push failed",
            created_at="t0",
            updated_at="t1",
        )

    def test_roundtrip(self) -> None:
        record = self._record()
        decoded = record_from_dict(record_to_dict(record))
        assert decoded == record
        assert decoded.git["branch"] == "sbx/a1"
        assert decoded.pull_request["ref"] == "refs/pull/7/head"
        assert decoded.pushed_head_sha == SHA_B
        assert decoded.merge["merge_commit_sha"] == SHA_0
        assert decoded.publish_error == "repo_unavailable: push failed"

    def test_none_fields_roundtrip(self) -> None:
        record = WorkspaceRecord(agent_id="a1", repo="/r", base_ref="main", base_sha=SHA_A)
        decoded = record_from_dict(record_to_dict(record))
        assert decoded.git is None
        assert decoded.branch is None
        assert decoded.pushed_head_sha is None
        assert decoded.pull_request is None
        assert decoded.merge is None
        assert decoded.publish_error is None

    @pytest.mark.parametrize(
        "patch",
        [
            {"pushed_head_sha": "notasha"},
            {"branch": "bad;rm"},
            {"branch": "-x"},
            {"git": "notadict"},
            {"git": {"unknown": 1}},
            {"git": {"push": "yes"}},
            {"pull_request": "notadict"},
            {"pull_request": {"number": "7"}},
            {"pull_request": {"number": True}},
            {"pull_request": {"head_sha": "zz"}},
            {"pull_request": {"draft": "no"}},
            {"pull_request": {"bogus": 1}},
            {"merge": "notadict"},
            {"merge": {"bogus": 1}},
            {"merge": {"merge_commit_sha": "zz"}},
            {"merge": {"merged": "yes"}},
            {"merge": {"merged_at": 3}},
            {"publish_error": 3},
            {"git": {"auto_publish": "yes"}},
            {"git": {"merge": "no"}},
        ],
    )
    def test_rejects_junk(self, patch: dict) -> None:
        data = record_to_dict(self._record()) | patch
        with pytest.raises(ValueError):
            record_from_dict(data)


class TestPrepareWithPolicy:
    def test_branch_materializes_on_checkout(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        record = workspaces.prepare(
            handle, "a1", spec(origin, base), git={"branch": "sbx/work", "push": True}
        )
        assert record.checkout_sha == base
        assert record.branch == "sbx/work"
        assert record.git is not None and record.git["branch"] == "sbx/work"
        assert record.git["target"] == "main"
        workdir = handle.root / "repo"
        assert host_git(workdir, "rev-parse", "--abbrev-ref", "HEAD") == "sbx/work"
        # Durable round-trip keeps the policy + branch.
        stored = workspaces.get("a1")
        assert stored is not None and stored.branch == "sbx/work"

    def test_default_branch_name(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        record = workspaces.prepare(handle, "agent-9", spec(origin, base), git={"push": True})
        assert record.branch == "sbx/agent-9"
        workdir = handle.root / "repo"
        assert host_git(workdir, "rev-parse", "--abbrev-ref", "HEAD") == "sbx/agent-9"

    def test_no_policy_detached_as_before(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        record = workspaces.prepare(handle, "a1", spec(origin, base))
        assert record.branch is None and record.git is None
        workdir = handle.root / "repo"
        assert host_git(workdir, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"

    def test_unsafe_branch_fails_before_clone(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        with pytest.raises(WorkspaceError) as exc:
            workspaces.prepare(handle, "a1", spec(origin, base), git={"branch": "bad;rm"})
        assert exc.value.code == WORKSPACE_INVALID
        assert workspaces.get("a1") is None


class TestPublish:
    def _prepared(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        git: dict[str, Any],
    ) -> tuple[Path, str, Path]:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base), git=git)
        workdir = handle.root / "repo"
        return origin, base, workdir

    def test_push_records_pushed_head_on_remote(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base, workdir = self._prepared(
            tmp_path, handle, workspaces, {"branch": "sbx/work", "push": True}
        )
        head = commit_file(workdir, "b.txt", "two\n")
        record = workspaces.publish(handle, "a1")
        assert record.head_sha == head != base
        assert record.pushed_head_sha == head
        assert record.pull_request is None
        # The remote really carries the branch at exactly the pushed head.
        assert host_git(origin, "rev-parse", "refs/heads/sbx/work") == head
        stored = workspaces.get("a1")
        assert stored is not None and stored.pushed_head_sha == head

    def test_publish_requires_policy(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base))
        with pytest.raises(WorkspaceError) as exc:
            workspaces.publish(handle, "a1")
        assert exc.value.code == WORKSPACE_INVALID

    def test_publish_requires_push(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base), git={"branch": "b"})
        with pytest.raises(WorkspaceError) as exc:
            workspaces.publish(handle, "a1")
        assert exc.value.code == WORKSPACE_INVALID

    def test_publish_unknown_agent(
        self, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        with pytest.raises(WorkspaceError) as exc:
            workspaces.publish(handle, "missing")
        assert exc.value.code == WORKSPACE_NOT_FOUND

    def test_remote_drift_fails_closed(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The push lands but ``ls-remote`` resolves a different sha — the
        publish must fail and ``pushed_head_sha`` must not be recorded."""
        _, _, workdir = self._prepared(
            tmp_path, handle, workspaces, {"branch": "sbx/work", "push": True}
        )
        commit_file(workdir, "b.txt", "two\n")
        monkeypatch.setattr("control.workspace.git_ls_remote", lambda *a, **kw: SHA_0)
        with pytest.raises(WorkspaceError) as exc:
            workspaces.publish(handle, "a1")
        assert exc.value.code == REPO_UNAVAILABLE
        assert workspaces.get("a1").pushed_head_sha is None

    def test_auto_create_pr_without_github_bridge(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        """PR creation needs the opt-in bridge; without it the publish
        still pushes but fails explicitly on the PR step (repo_unavailable)."""
        origin, _, workdir = self._prepared(
            tmp_path,
            handle,
            workspaces,
            {"branch": "sbx/work", "push": True, "auto_create_pr": True},
        )
        head = commit_file(workdir, "b.txt", "two\n")
        with pytest.raises(WorkspaceError) as exc:
            workspaces.publish(handle, "a1")
        assert exc.value.code == REPO_UNAVAILABLE
        assert workspaces.get("a1").pushed_head_sha == head  # push verified
        assert workspaces.get("a1").pull_request is None

    def test_auto_create_pr_records_metadata(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[dict[str, Any]] = []

        def fake_pr(backend: Any, h: Any, repo: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"number": 7, "html_url": "https://gh.test/pr/7", "state": "open"}

        monkeypatch.setattr("control.workspace.create_pull_request", fake_pr)
        origin, _, workdir = self._prepared(
            tmp_path,
            handle,
            workspaces,
            {
                "branch": "sbx/work",
                "push": True,
                "auto_create_pr": True,
                "target": "main",
                "draft": True,
                "title": "My PR",
            },
        )
        head = commit_file(workdir, "b.txt", "two\n")
        record = workspaces.publish(handle, "a1")
        pr = record.pull_request
        assert pr is not None
        assert pr["number"] == 7
        assert pr["url"] == "https://gh.test/pr/7"
        assert pr["ref"] == "refs/pull/7/head"
        assert pr["head_sha"] == head
        assert pr["base"] == "main"
        assert pr["draft"] is True
        assert calls[0]["head"] == "sbx/work"
        assert calls[0]["base"] == "main"
        assert calls[0]["title"] == "My PR"
        assert calls[0]["draft"] is True

        # A second publish re-pins the recorded PR head; it does not
        # open a duplicate PR.
        head2 = commit_file(workdir, "c.txt", "three\n")
        record = workspaces.publish(handle, "a1")
        assert record.pull_request["head_sha"] == head2
        assert len(calls) == 1


class TestReviewComment:
    def _with_pr(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Path:
        monkeypatch.setattr(
            "control.workspace.create_pull_request",
            lambda *a, **kw: {"number": 7, "html_url": "https://gh.test/pr/7"},
        )
        origin, base = make_repo(tmp_path)
        workspaces.prepare(
            handle,
            "a1",
            spec(origin, base),
            git={"branch": "sbx/work", "push": True, "auto_create_pr": True},
        )
        workdir = handle.root / "repo"
        commit_file(workdir, "b.txt", "two\n")
        workspaces.publish(handle, "a1")
        return workdir

    def test_comment_records_url(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._with_pr(tmp_path, handle, workspaces, monkeypatch)
        seen: list[dict[str, Any]] = []

        def fake_comment(backend: Any, h: Any, repo: str, **kwargs: Any) -> dict[str, Any]:
            seen.append(kwargs)
            return {"html_url": "https://gh.test/pr/7#c1"}

        monkeypatch.setattr("control.workspace.create_issue_comment", fake_comment)
        record = workspaces.post_review_comment(handle, "a1", "machine review: pass")
        assert record.pull_request["review_comment_url"] == "https://gh.test/pr/7#c1"
        assert seen[0]["number"] == 7
        assert seen[0]["body"] == "machine review: pass"

    def test_comment_requires_pr(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base), git={"push": True})
        with pytest.raises(WorkspaceError) as exc:
            workspaces.post_review_comment(handle, "a1", "review")
        assert exc.value.code == WORKSPACE_INVALID

    def test_comment_requires_body(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._with_pr(tmp_path, handle, workspaces, monkeypatch)
        with pytest.raises(WorkspaceError) as exc:
            workspaces.post_review_comment(handle, "a1", "")
        assert exc.value.code == WORKSPACE_INVALID


class TestPullRequestHandoff:
    """Reviewer start: fetch a remote ref pinned to an exact head."""

    def _pr_repo(self, tmp_path: Path, ref: str = "refs/pull/7/head") -> tuple[Path, str, str]:
        """Origin on ``main`` (base) + a descendant commit pinned at ``ref``."""
        origin, base = make_repo(tmp_path)
        host_git(origin, "checkout", "-qb", "work")
        head = commit_file(origin, "b.txt", "two\n")
        host_git(origin, "update-ref", ref, head)
        host_git(origin, "checkout", "-q", "main")
        return origin, base, head

    @pytest.mark.parametrize("ref", ["refs/pull/7/head", "pull/7/head", "work"])
    def test_fetch_pin_checkout(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        ref: str,
    ) -> None:
        origin, base, head = self._pr_repo(tmp_path)
        record = handoff.prepare_from_pull_request(handle, "a1", ref, head, spec=spec(origin, base))
        assert record.checkout_sha == head
        assert record.head_sha == head
        assert host_git(handle.root / "repo", "rev-parse", "HEAD") == head
        assert (handle.root / "repo" / "b.txt").read_text() == "two\n"

    def test_ref_drift_fails_closed(
        self, tmp_path: Path, handle: SandboxHandle, handoff: HandoffService
    ) -> None:
        """The ref moved since the pin was taken — explicit mismatch, never
        a checkout of the drifted commit."""
        origin, base, head = self._pr_repo(tmp_path)
        host_git(origin, "update-ref", "refs/pull/7/head", base)  # drift
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_pull_request(
                handle, "a1", "refs/pull/7/head", head, spec=spec(origin, base)
            )
        assert exc.value.code == HEAD_SHA_MISMATCH
        # The workspace prepared the declared base but never the drifted head.
        record = handoff._workspaces.get("a1")
        assert record is not None and record.head_sha == base
        assert host_git(handle.root / "repo", "rev-parse", "HEAD") == base

    def test_unknown_ref_fails(
        self, tmp_path: Path, handle: SandboxHandle, handoff: HandoffService
    ) -> None:
        origin, base = make_repo(tmp_path)
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_pull_request(
                handle, "a1", "refs/pull/99/head", SHA_A, spec=spec(origin, base)
            )
        assert exc.value.code == REPO_UNAVAILABLE

    @pytest.mark.parametrize("ref", ["bad;rm", "a..b", "-x", "a b"])
    def test_unsafe_ref_rejected_before_fetch(
        self, tmp_path: Path, handle: SandboxHandle, handoff: HandoffService, ref: str
    ) -> None:
        origin, base, head = self._pr_repo(tmp_path)
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_pull_request(handle, "a1", ref, head, spec=spec(origin, base))
        assert exc.value.code == WORKSPACE_INVALID

    def test_bad_head_sha_rejected(
        self, tmp_path: Path, handle: SandboxHandle, handoff: HandoffService
    ) -> None:
        origin, base = make_repo(tmp_path)
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_pull_request(
                handle, "a1", "refs/pull/7/head", "notasha", spec=spec(origin, base)
            )
        assert exc.value.code == WORKSPACE_INVALID

    def test_head_not_descending_from_base(
        self, tmp_path: Path, handle: SandboxHandle, handoff: HandoffService
    ) -> None:
        origin, base = make_repo(tmp_path)
        host_git(origin, "checkout", "-q", "--orphan", "other")
        host_git(origin, "rm", "-q", "-rf", ".")
        host_git(origin, "commit", "-qm", "D", "--allow-empty")
        other = host_git(origin, "rev-parse", "other")
        host_git(origin, "update-ref", "refs/pull/8/head", other)
        host_git(origin, "checkout", "-q", "main")
        with pytest.raises(WorkspaceError) as exc:
            handoff.prepare_from_pull_request(
                handle, "a1", "refs/pull/8/head", other, spec=spec(origin, base)
            )
        assert exc.value.code == BASE_SHA_MISMATCH

    def test_onto_existing_workspace(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
        workspaces: WorkspaceService,
    ) -> None:
        """Live handoff onto an already-prepared workspace (the
        ``POST /v1/agents/{id}/handoff`` path): the recorded branch policy
        carries into the checkout."""
        origin, base, head = self._pr_repo(tmp_path)
        workspaces.prepare(handle, "a1", spec(origin, base), git={"branch": "sbx/review"})
        record = handoff.prepare_from_pull_request(handle, "a1", "refs/pull/7/head", head)
        assert record.head_sha == head
        workdir = handle.root / "repo"
        assert host_git(workdir, "rev-parse", "--abbrev-ref", "HEAD") == "sbx/review"

    def test_policy_checkout_preserves_branch(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        handoff: HandoffService,
    ) -> None:
        """A head_sha handoff onto a policy workspace also lands on the
        declared branch rather than a detached head."""
        origin, base = make_repo(tmp_path)
        host_git(origin, "checkout", "-qb", "work")
        head = commit_file(origin, "b.txt", "two\n")
        host_git(origin, "checkout", "-q", "main")
        record = handoff.prepare_from_head(
            handle,
            "a1",
            head,
            spec=spec(origin, base),
            git={"branch": "sbx/work"},
        )
        assert record.head_sha == head
        workdir = handle.root / "repo"
        assert host_git(workdir, "rev-parse", "--abbrev-ref", "HEAD") == "sbx/work"


class TestPublishError:
    """SOR-178: publish failures land durably on the record; success clears."""

    def test_failure_recorded_then_cleared(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        origin, _, workdir = TestPublish()._prepared(
            tmp_path,
            handle,
            workspaces,
            {"branch": "sbx/work", "push": True, "auto_create_pr": True},
        )
        commit_file(workdir, "b.txt", "two\n")
        # No GitHub bridge → the PR step fails; the record keeps the error.
        with pytest.raises(WorkspaceError) as exc:
            workspaces.publish(handle, "a1")
        assert exc.value.code == REPO_UNAVAILABLE
        stored = workspaces.get("a1")
        assert stored.publish_error is not None
        assert stored.publish_error.startswith("repo_unavailable:")

        monkeypatch.setattr(
            "control.workspace.create_pull_request",
            lambda *a, **kw: {"number": 7, "html_url": "https://gh.test/pr/7", "state": "open"},
        )
        record = workspaces.publish(handle, "a1")
        assert record.publish_error is None
        assert workspaces.get("a1").publish_error is None

    def test_merged_pr_recreated_on_next_publish(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[dict[str, Any]] = []

        def fake_pr(backend: Any, h: Any, repo: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"number": 6 + len(calls), "html_url": "https://gh.test/pr", "state": "open"}

        monkeypatch.setattr("control.workspace.create_pull_request", fake_pr)
        origin, _, workdir = TestPublish()._prepared(
            tmp_path,
            handle,
            workspaces,
            {"branch": "sbx/work", "push": True, "auto_create_pr": True},
        )
        commit_file(workdir, "b.txt", "two\n")
        record = workspaces.publish(handle, "a1")
        # A terminal PR cannot track the branch — a fresh publish opens a
        # new one instead of re-pinning the merged record.
        record.pull_request["state"] = "merged"
        workspaces.save(record)
        commit_file(workdir, "c.txt", "three\n")
        record = workspaces.publish(handle, "a1")
        assert len(calls) == 2
        assert record.pull_request["number"] == 8


class TestMerge:
    """SOR-178 review-gated merge: pin + recorded-head + remote-head must
    all agree, then the durable merge metadata is persisted."""

    def _with_pr(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
        git: dict[str, Any] | None = None,
    ) -> tuple[Path, Path, str]:
        """Prepared workspace + published PR #7 whose remote ref is pinned
        at the pushed head. Returns (origin, workdir, head)."""
        monkeypatch.setattr(
            "control.workspace.create_pull_request",
            lambda *a, **kw: {"number": 7, "html_url": "https://gh.test/pr/7", "state": "open"},
        )
        origin, base = make_repo(tmp_path)
        workspaces.prepare(
            handle,
            "a1",
            spec(origin, base),
            git=git or {"branch": "sbx/work", "push": True, "auto_create_pr": True, "merge": True},
        )
        workdir = handle.root / "repo"
        head = commit_file(workdir, "b.txt", "two\n")
        workspaces.publish(handle, "a1")
        # The remote PR ref tracks the branch head (GitHub-maintained).
        host_git(origin, "update-ref", "refs/pull/7/head", head)
        return origin, workdir, head

    def test_merge_success_persists_metadata(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        origin, workdir, head = self._with_pr(tmp_path, handle, workspaces, monkeypatch)
        workspaces.mark_reviewed("a1", head)
        seen: list[dict[str, Any]] = []

        def fake_merge(backend: Any, h: Any, repo: str, **kwargs: Any) -> dict[str, Any]:
            seen.append(kwargs)
            return {"merged": True, "sha": SHA_0, "message": "merged"}

        monkeypatch.setattr("control.workspace.merge_pull_request", fake_merge)
        record = workspaces.merge(handle, "a1")
        assert seen[0]["number"] == 7
        assert seen[0]["sha"] == head  # GitHub gets the reviewed-sha pin too
        assert record.merge is not None
        assert record.merge["merged"] is True
        assert record.merge["merge_commit_sha"] == SHA_0
        assert record.merge["head_sha"] == head
        assert record.merge["merged_at"]
        assert record.pull_request["state"] == "merged"
        # Durable: the merge record survives a store round-trip.
        decoded = record_from_dict(record_to_dict(workspaces.get("a1")))
        assert decoded.merge == record.merge

        # Idempotent: a second call does not hit GitHub again.
        record = workspaces.merge(handle, "a1")
        assert record.merge["merged"] is True
        assert len(seen) == 1

    def test_merge_requires_policy(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        origin, workdir, head = self._with_pr(
            tmp_path,
            handle,
            workspaces,
            monkeypatch,
            git={"branch": "sbx/work", "push": True, "auto_create_pr": True},
        )
        workspaces.mark_reviewed("a1", head)
        with pytest.raises(WorkspaceError) as exc:
            workspaces.merge(handle, "a1")
        assert exc.value.code == WORKSPACE_INVALID

    def test_merge_requires_recorded_pr(
        self, tmp_path: Path, handle: SandboxHandle, workspaces: WorkspaceService
    ) -> None:
        origin, base = make_repo(tmp_path)
        workspaces.prepare(
            handle,
            "a1",
            spec(origin, base),
            git={"push": True, "auto_create_pr": True, "merge": True},
        )
        with pytest.raises(WorkspaceError) as exc:
            workspaces.merge(handle, "a1")
        assert exc.value.code == WORKSPACE_INVALID

    def test_merge_requires_review_pin(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._with_pr(tmp_path, handle, workspaces, monkeypatch)
        with pytest.raises(WorkspaceError) as exc:
            workspaces.merge(handle, "a1")
        assert exc.value.code == REVIEW_REQUIRED

    def test_recorded_head_drift_requires_rereview(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        origin, workdir, head = self._with_pr(tmp_path, handle, workspaces, monkeypatch)
        workspaces.mark_reviewed("a1", head)
        # New work pushed after the pin moved the recorded PR head.
        head2 = commit_file(workdir, "c.txt", "three\n")
        workspaces.publish(handle, "a1")
        host_git(origin, "update-ref", "refs/pull/7/head", head2)
        with pytest.raises(WorkspaceError) as exc:
            workspaces.merge(handle, "a1")
        assert exc.value.code == HEAD_SHA_MISMATCH
        # A fresh review of the new head unblocks the merge.
        workspaces.mark_reviewed("a1", head2)
        monkeypatch.setattr(
            "control.workspace.merge_pull_request",
            lambda *a, **kw: {"merged": True, "sha": SHA_0},
        )
        record = workspaces.merge(handle, "a1")
        assert record.merge["head_sha"] == head2

    def test_remote_head_drift_fails_closed(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The remote PR ref moved since the pin (someone pushed to the PR)
        — the merge must refuse even though our records still agree."""
        origin, workdir, head = self._with_pr(tmp_path, handle, workspaces, monkeypatch)
        workspaces.mark_reviewed("a1", head)
        head2 = commit_file(origin, "d.txt", "four\n")
        host_git(origin, "update-ref", "refs/pull/7/head", head2)
        called: list[Any] = []
        monkeypatch.setattr(
            "control.workspace.merge_pull_request",
            lambda *a, **kw: called.append(1) or {"merged": True},
        )
        with pytest.raises(WorkspaceError) as exc:
            workspaces.merge(handle, "a1")
        assert exc.value.code == HEAD_SHA_MISMATCH
        assert not called  # GitHub merge never attempted
        assert workspaces.get("a1").merge is None

    def test_merge_github_failure_persists_nothing(
        self,
        tmp_path: Path,
        handle: SandboxHandle,
        workspaces: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        origin, workdir, head = self._with_pr(tmp_path, handle, workspaces, monkeypatch)
        workspaces.mark_reviewed("a1", head)
        monkeypatch.setattr(
            "control.workspace.merge_pull_request",
            lambda *a, **kw: {"merged": False, "message": "not mergeable"},
        )
        with pytest.raises(WorkspaceError) as exc:
            workspaces.merge(handle, "a1")
        assert exc.value.code == REPO_UNAVAILABLE
        record = workspaces.get("a1")
        assert record.merge is None
        assert record.pull_request["state"] == "open"
