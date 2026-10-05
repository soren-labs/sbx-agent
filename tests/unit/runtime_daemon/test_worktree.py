"""Worktree materialization (RFC 167 §03) — local git, no network."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from runtime.daemon.worktree import ensure_worktree


def _src_repo(tmp_path: Path) -> tuple[Path, str]:
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=src, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=src, check=True)
    (src / "hello.txt").write_text("v1\n")
    subprocess.run(["git", "add", "."], cwd=src, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=src, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src, check=True, capture_output=True, text=True
    ).stdout.strip()
    return src, head


def _spec(src: Path, branch: str = "sbx/sess_1") -> dict:
    return {
        "repository": "org/repo",
        "base_ref": "main",
        "branch": branch,
        "remote_url": f"file://{src}",
    }


def test_fresh_clone_materializes_repo(tmp_path):
    src, head = _src_repo(tmp_path)
    wt = tmp_path / "worktree"
    out = ensure_worktree(wt, _spec(src), {})
    assert out["fresh_clone"] is True
    assert out["head_sha"] == head
    assert (wt / "hello.txt").read_text() == "v1\n"
    # remote URL never carries credentials
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=wt, capture_output=True, text=True
    ).stdout.strip()
    assert remote == f"file://{src}"
    # work branch checked out
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=wt, capture_output=True, text=True
    ).stdout.strip()
    assert branch == "sbx/sess_1"


def test_resume_is_idempotent_and_refetches(tmp_path):
    src, head = _src_repo(tmp_path)
    wt = tmp_path / "worktree"
    ensure_worktree(wt, _spec(src), {})
    (wt / "local.txt").write_text("dirty\n")
    out = ensure_worktree(wt, _spec(src), {})
    assert out["fresh_clone"] is False
    assert out["head_sha"] == head
    # Tracked content still matches the fetched ref; untracked artifacts persist.
    assert (wt / "hello.txt").read_text() == "v1\n"


def test_event_hook_receives_ready(tmp_path):
    src, head = _src_repo(tmp_path)
    seen = []
    out = ensure_worktree(
        tmp_path / "wt",
        _spec(src),
        {},
        event=lambda kind, payload: seen.append((kind, payload)),
    )
    assert seen[0][0] == "worktree.ready"
    assert seen[0][1]["head_sha"] == out["head_sha"]


def test_empty_repo_skips(tmp_path):
    assert ensure_worktree(tmp_path / "wt", {}, {}) == {"skipped": "no repository declared"}
    assert not (tmp_path / "wt" / ".git").exists()


def test_failed_fetch_surfaces_error(tmp_path):
    with pytest.raises(RuntimeError, match="git"):
        ensure_worktree(
            tmp_path / "wt",
            {
                "repository": "org/repo",
                "base_ref": "main",
                "branch": "b",
                "remote_url": "file:///nonexistent-repo-xyz",
            },
            {},
        )
