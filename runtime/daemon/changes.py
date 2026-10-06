"""Quiescent ChangeSet capture and fenced apply against the logical Worktree.

Capture stages tracked/untracked/deleted/binary/symlink changes into a private
index (never the user's index), writes the exact resulting tree, and reports a
canonical manifest with per-file content digests plus a binary patch.
Credential paths are excluded and credential values (the lease's known secrets and
obvious token/private-key patterns) fail capture (``runtime.security.artifacts``).
"""

from __future__ import annotations

import base64
import os
import subprocess
from collections.abc import Iterable
from typing import Any

from protocol.manifests import MANIFEST_VERSION, canonical_manifest, content_digest, subject_digest

from runtime.daemon.worktree import Worktree, WorktreeError, git
from runtime.security.artifacts import git_excludes, git_templates, is_secret_path, secret_reason

MAX_PAYLOAD = 50 * 1024 * 1024
EXCLUDES = (*git_excludes(), ":(exclude).sbx")


def _index_env(worktree: Worktree) -> dict[str, str]:
    index = worktree.state_dir / "capture.index"
    index.unlink(missing_ok=True)
    return {"GIT_INDEX_FILE": str(index)}


def baseline_rev(worktree: Worktree) -> str:
    marker = worktree.state_dir / "baseline_rev"
    if marker.exists():
        return marker.read_text().strip()
    return git(worktree.path, "rev-parse", "HEAD").stdout.strip()


def working_tree(worktree: Worktree, base: str) -> str:
    env = _index_env(worktree)
    git(worktree.path, "read-tree", base, env=env)
    git(worktree.path, "add", "-A", "--", ".", *EXCLUDES, env=env)
    # Templates such as .env.example are documentation, not credentials: stage them.
    listed = git(
        worktree.path,
        "ls-files",
        "-z",
        "--others",
        "--modified",
        "--deleted",
        "--exclude-standard",
        "--",
        *git_templates(),
        env=env,
    ).stdout.split("\0")
    templates = sorted({p for p in listed if p and not is_secret_path(p)})
    if templates:
        git(worktree.path, "add", "-A", "--", *templates, env=env)
    return git(worktree.path, "write-tree", env=env).stdout.strip()


def _blob(worktree: Worktree, tree: str, path: str) -> tuple[str, bytes]:
    entry = git(worktree.path, "ls-tree", "-z", tree, "--", path).stdout.rstrip("\0")
    meta, _ = entry.split("\t", 1)
    mode, _kind, oid = meta.split()
    data = subprocess.run(
        ["git", "cat-file", "blob", oid], cwd=worktree.path, capture_output=True, check=True
    ).stdout
    return mode, data


def capture(
    worktree: Worktree, payload: dict[str, Any], known: Iterable[str] = ()
) -> dict[str, Any]:
    if not worktree.path.exists():
        raise WorktreeError("executor_unavailable", "worktree not realized")
    expected = payload.get("expected_generation")
    if expected is not None and int(expected) != worktree.generation:
        raise WorktreeError("version_conflict", "worktree generation changed since capture request")
    base_sha = payload["base_sha"]
    baseline = baseline_rev(worktree)
    baseline_tree = git(worktree.path, "rev-parse", f"{baseline}^{{tree}}").stdout.strip()
    tree = working_tree(worktree, baseline)
    raw = git(
        worktree.path, "diff", "--name-status", "--no-renames", "-z", baseline_tree, tree
    ).stdout.split("\0")
    files: list[dict[str, Any]] = []
    blobs: dict[str, str] = {}
    total = 0
    for status, path in zip(raw[0::2], raw[1::2], strict=False):
        if not path:
            continue
        if is_secret_path(path):
            raise WorktreeError("capture_failed", f"credential path {path}; capture refused")
        if status == "D":
            files.append({"path": path, "type": "deleted"})
            continue
        mode, data = _blob(worktree, tree, path)
        reason = secret_reason(data, known)
        if reason:
            raise WorktreeError("capture_failed", f"{reason} in {path}; capture refused")
        digest = content_digest(data)
        total += len(data)
        if total > MAX_PAYLOAD:
            raise WorktreeError("capture_failed", "changeset payload exceeds limit")
        blobs[digest] = base64.b64encode(data).decode()
        kind = "symlink" if mode == "120000" else "file"
        files.append({"path": path, "type": kind, "mode": mode, "digest": digest})
    patch = subprocess.run(
        ["git", "diff", "--binary", "--full-index", "--no-renames", baseline_tree, tree],
        cwd=worktree.path,
        capture_output=True,
        check=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(worktree.state_dir)},
    ).stdout
    reason = secret_reason(patch, known)
    if reason:
        raise WorktreeError("capture_failed", f"{reason} in patch; capture refused")
    manifest = canonical_manifest(
        repository=payload.get("repository"),
        base_sha=base_sha,
        baseline_tree=baseline_tree,
        files=files,
        tree_sha=tree,
    )
    return {
        "manifest_version": MANIFEST_VERSION,
        "manifest": manifest,
        "subject_digest": subject_digest(manifest),
        "patch_b64": base64.b64encode(patch).decode(),
        "patch_digest": content_digest(patch),
        "blobs": blobs,
        "generation": worktree.generation,
    }


def apply(worktree: Worktree, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply an immutable ChangeSet patch only onto its exact baseline (no partial writes)."""
    expected = payload.get("expected_generation")
    if expected is not None and int(expected) != worktree.generation:
        raise WorktreeError("version_conflict", "destination generation changed")
    patch = base64.b64decode(payload["patch_b64"])
    if content_digest(patch) != payload["patch_digest"]:
        raise WorktreeError("capture_failed", "patch digest mismatch")
    current = working_tree(worktree, baseline_rev(worktree))
    if payload.get("baseline_tree") and current != payload["baseline_tree"]:
        raise WorktreeError("stale_subject", "destination files differ from the ChangeSet baseline")
    patch_file = worktree.state_dir / "apply.patch"
    patch_file.write_bytes(patch)
    if patch:
        check = subprocess.run(
            ["git", "apply", "--check", "--binary", str(patch_file)],
            cwd=worktree.path,
            capture_output=True,
            text=True,
        )
        if check.returncode != 0:
            raise WorktreeError("stale_subject", "patch does not apply cleanly; nothing changed")
        git(worktree.path, "apply", "--binary", str(patch_file))
    if payload.get("commit_as_baseline"):
        git(worktree.path, "add", "-A")
        git(
            worktree.path,
            "-c",
            "user.name=sbx",
            "-c",
            "user.email=sbx@localhost",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            f"sbx input {payload.get('changeset_id', '')}",
        )
        head = git(worktree.path, "rev-parse", "HEAD").stdout.strip()
        (worktree.state_dir / "baseline_rev").write_text(head)
    return {"generation": worktree.bump(), "tree": working_tree(worktree, baseline_rev(worktree))}
