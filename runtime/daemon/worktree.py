"""Worktree materialization inside the executor (RFC 167 §03).

A Session's ``projectless_spec`` / ProjectVersion names a ``repository``
+ ``base_ref``; the worktree boundary is where that declaration becomes
real bytes. The first ``turn.start`` materializes the repo (idempotent —
later turns and resumptions skip a healthy ``.git``), then checks out the
Session's work branch.

The clone token arrives via the ``git_credentials`` payload channel
(``env.GIT_AUTH_TOKEN``) and is passed to git only through
``-c http.extraHeader`` so it is never written to ``.git/config``,
logged, or placed on a remote URL.
"""

from __future__ import annotations

import base64
import subprocess
import urllib.parse
from pathlib import Path

# Non-secret git env the helper may export; the token lives in
# GIT_AUTH_TOKEN and is only interpolated into -c headers.
_TIMEOUT_S = 120


def _auth_header(token: str, remote: str) -> list[str]:
    """HTTP auth via -c extraheader scoped to the remote's host.

    GitHub tokens (PAT, fine-grained, OAuth, App installation) all accept
    Basic ``x-access-token:<token>`` over git smart-HTTP; Bearer is
    rejected by ghs_ installation tokens. The header is base64'd so the
    token's charset can't corrupt the -c syntax, and it is scoped to the
    remote host so it can never leak to a different origin.
    """
    host = urllib.parse.urlparse(remote).netloc or "github.com"
    scheme = urllib.parse.urlparse(remote).scheme or "https"
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return [
        "-c",
        f"http.{scheme}://{host}/.extraheader=AUTHORIZATION: basic {auth}",
    ]


def _git(
    root: Path,
    *argv: str,
    env: dict | None = None,
    header_token: str | None = None,
    remote: str = "",
) -> str:
    cmd = ["git"]
    if header_token:
        cmd += _auth_header(header_token, remote)
    cmd += list(argv)
    proc = subprocess.run(
        cmd,
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
    )
    if proc.returncode != 0:
        # Never include the token in the error surface.
        raise RuntimeError(f"git {' '.join(argv)} failed: {proc.stderr[-400:]}")
    return proc.stdout.strip()


def ensure_worktree(
    worktree_root: Path,
    spec: dict,
    git_env: dict,
    *,
    event=None,
) -> dict:
    """Idempotently materialize ``spec`` into ``worktree_root``.

    ``spec``: ``{repository, base_ref, branch}``. ``git_env`` may carry
    ``GIT_AUTH_TOKEN`` (used for fetch only). Returns the resulting state
    ``{repository, base_ref, branch, head_sha, fresh_clone}``.
    """
    repo = str(spec.get("repository") or "")
    if not repo:
        return {"skipped": "no repository declared"}
    base_ref = str(spec.get("base_ref") or "main")
    branch = str(spec.get("branch") or "sbx-work")
    token = git_env.get("GIT_AUTH_TOKEN")
    root = Path(worktree_root)
    root.mkdir(parents=True, exist_ok=True)

    git_dir = root / ".git"
    remote = str(spec.get("remote_url") or f"https://github.com/{repo}.git")
    fresh = not git_dir.exists()
    if fresh:
        _git(root, "init", "-q")
        # Token-free remote URL — auth flows through -c extraheader only.
        _git(root, "remote", "add", "origin", remote)

    def fetch() -> None:
        _git(
            root,
            "fetch",
            "--depth=50",
            "origin",
            base_ref,
            env=None,
            header_token=token,
            remote=remote,
        )

    fetch()
    try:
        _git(root, "checkout", "-q", "-B", branch, "FETCH_HEAD")
    except RuntimeError:
        # Dirty tracked state blocks checkout — the worktree is executor-local,
        # so reset hard rather than wedge the session.
        _git(root, "reset", "-q", "--hard", "FETCH_HEAD")
        _git(root, "checkout", "-q", "-B", branch, "HEAD")
    head = _git(root, "rev-parse", "HEAD")
    if event is not None:
        event(
            "worktree.ready",
            {
                "repository": repo,
                "base_ref": base_ref,
                "branch": branch,
                "head_sha": head,
                "fresh_clone": fresh,
            },
        )
    return {
        "repository": repo,
        "base_ref": base_ref,
        "branch": branch,
        "head_sha": head,
        "fresh_clone": fresh,
    }
