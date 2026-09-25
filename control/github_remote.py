"""SOR-225: control-plane GitHub integration + host-side payload delivery.

Two seams, both running on the control plane host — never inside a sandbox:

* ``RemoteGitHub`` is the server-side REST client for pull-request
  status / create / comment / merge. Its token comes from the same sources
  as the sandbox injection bridge — an operator PAT (``GH_TOKEN`` /
  ``GITHUB_TOKEN``) or a GitHub App installation token minted by
  ``control.github_app`` — resolved by :func:`server_token`. Operations run
  unauthenticated only when no source exists, so public repos keep working
  and private ones fail closed instead of silently downgrading.

* :func:`push_payload` delivers a durable Revision's artifact payload to the
  remote without a live sandbox: it rebuilds git objects in a scratch repo
  (exact commits from a ``repo.bundle``, or base + applied ``patch.diff``),
  pushes the result, and verifies the remote head. Token material travels
  through subprocess env only (``GH_TOKEN`` + a credential helper), never in
  argv or on disk.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from control import github

API_URL_ENV = "SBX_GITHUB_API_URL"
DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"

_GIT_TIMEOUT_S = 120.0
_HTTP_TIMEOUT_S = 15.0

_PULL_URL_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)/?$")

# Same credential-helper mechanism as the sandbox bridge: git asks the
# helper, the helper echoes ``$GH_TOKEN`` — the token never appears in argv,
# in the repo's config, or on disk.
_CREDENTIAL_KEYS = (
    ("credential.helper", ""),
    (
        "credential.https://github.com.helper",
        '!f() { echo "username=x-access-token"; echo "password=$GH_TOKEN"; }; f',
    ),
)


class RemoteGitHubError(Exception):
    """A failed server-side GitHub/git operation carrying a canonical code.

    Codes mirror the ``WorkspaceError`` taxonomy so API surfaces map them
    without translation.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_pull_url(url: Any) -> tuple[str, int] | None:
    """``(owner/repo, number)`` from a github.com PR URL, else ``None``."""
    if not isinstance(url, str):
        return None
    match = _PULL_URL_RE.match(url.strip())
    if match is None:
        return None
    return f"{match.group(1)}/{match.group(2)}", int(match.group(3))


def server_token(repo: str | None = None, env: Mapping[str, str] | None = None) -> str | None:
    """Resolve a control-plane GitHub token; ``None`` when no source exists.

    Env PAT wins; otherwise a configured GitHub App mints a repo-scoped
    installation token. Unlike the sandbox bridge this is not gated on
    ``SBX_GITHUB_EPHEMERAL`` — that flag guards *injection into sandboxes*;
    the control plane using its own configured credential is the point of
    the integration.
    """
    env = os.environ if env is None else env
    token = github.resolve_token(env)
    if token is not None:
        return token
    from control import github_app

    return github_app.sandbox_token(env, repo=repo)


def host_git_env(repo: str | None = None, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Subprocess env for host-side git: minimal, prompt-free, token in env.

    Mirrors ``control.tasks._git_env`` (no credential bleed from the ambient
    environment) plus the credential-helper overlay when the repo is a
    github.com remote and a server token resolved.
    """
    env = os.environ if env is None else env
    out: dict[str, str] = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": env.get("HOME") or os.environ.get("HOME") or "",
        "PATH": env.get("PATH") or os.environ.get("PATH") or os.defpath,
    }
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"):
        if env.get(key):
            out[key] = env[key]  # type: ignore[index]
    if repo is not None and github.repo_slug(repo) is not None:
        token = server_token(repo, env)
        if token is not None:
            out["GH_TOKEN"] = token
            out["GITHUB_TOKEN"] = token
            for index, (key, value) in enumerate(_CREDENTIAL_KEYS):
                out[f"GIT_CONFIG_KEY_{index}"] = key
                out[f"GIT_CONFIG_VALUE_{index}"] = value
            out["GIT_CONFIG_COUNT"] = str(len(_CREDENTIAL_KEYS))
    return out


def _run_git(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str],
    git: str = "git",
    timeout_s: float = _GIT_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [git, *args],
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RemoteGitHubError("repo_unavailable", f"git {' '.join(args[:1])} failed: {exc}")


def _must(
    proc: subprocess.CompletedProcess[str], code: str, what: str
) -> subprocess.CompletedProcess[str]:
    if proc.returncode != 0:
        # stderr may echo the remote URL — redact credentials before
        # surfacing a clipped line, never the raw output.
        detail = github.redact_url_credentials(
            (proc.stderr or "").strip().splitlines()[-1] if proc.stderr else ""
        )
        raise RemoteGitHubError(
            code, f"{what} (exit {proc.returncode})" + (f": {detail[:200]}" if detail else "")
        )
    return proc


def ls_remote(repo: str, ref: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """Remote ``ref`` → commit sha, or ``None`` when it does not resolve."""
    proc = _run_git(["ls-remote", repo, ref], env=host_git_env(repo, env))
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].strip() == ref:
            sha = parts[0].strip()
            return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None
    return None


def push_payload(
    repo: str,
    branch: str,
    *,
    kind: str,
    payload: bytes,
    base_sha: str,
    head_sha: str,
    base_ref: str | None = None,
    env: Mapping[str, str] | None = None,
    git: str = "git",
    commit_date: str | None = None,
) -> str:
    """Push a durable revision's payload to ``repo``'s ``branch``; host-side.

    ``kind="bundle"`` fetches the exact commits out of a ``repo.bundle``
    member — the pushed sha is the recorded ``head_sha`` verbatim.
    ``kind="patch"`` applies ``patch.diff`` on top of the remote's
    ``base_sha`` and commits it as the delivery bot — the pushed sha is the
    new commit, returned so the caller records what actually landed.

    ``commit_date`` (ISO-8601, e.g. the revision's ``created_at``) pins
    ``GIT_AUTHOR_DATE``/``GIT_COMMITTER_DATE`` on the patch commit so a
    retried delivery of the same revision re-mints the identical sha —
    the push then converges ("Everything up-to-date") instead of
    non-fast-forward failing on its own previous commit.

    Fails closed at every step: an unfetchable base, a missing bundle head,
    an unapplying patch, or a remote head that disagrees after push is an
    explicit ``RemoteGitHubError`` — nothing is recorded on drift.
    """
    shell_env = dict(host_git_env(repo, env))
    if commit_date:
        try:
            datetime.fromisoformat(commit_date.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            commit_date = None
    if commit_date:
        shell_env["GIT_AUTHOR_DATE"] = commit_date
        shell_env["GIT_COMMITTER_DATE"] = commit_date

    def git_run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return _run_git(args, cwd=cwd, env=shell_env, git=git)

    with tempfile.TemporaryDirectory(prefix="sbx-deliver-") as tmp:
        work = Path(tmp)
        _must(git_run(["init", "-q"], work), "repo_unavailable", "git init failed")
        _must(
            git_run(["remote", "add", "origin", repo], work),
            "repo_unavailable",
            "git remote add failed",
        )
        # Fetch the remote base first: the scratch repo needs the base
        # objects to un-thin a bundle or apply a patch, and the fetch also
        # proves remote reachability before anything is pushed.
        fetch_ref = base_ref or "HEAD"
        proc = git_run(["fetch", "--no-tags", "-q", "origin", fetch_ref], work)
        _must(proc, "repo_unavailable", f"git fetch {fetch_ref} failed")
        proc = git_run(["cat-file", "-e", f"{base_sha}^{{commit}}"], work)
        if proc.returncode != 0:
            raise RemoteGitHubError(
                "base_sha_mismatch",
                f"base {base_sha} is not reachable from remote ref {fetch_ref}",
            )
        if kind == "bundle":
            bundle = work / "payload.bundle"
            bundle.write_bytes(payload)
            heads = git_run(["bundle", "list-heads", bundle], work)
            _must(heads, "artifact_invalid", "payload is not a git bundle")
            ref = None
            for line in heads.stdout.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[0] == head_sha:
                    ref = parts[1].strip()
                    break
            if ref is None:
                raise RemoteGitHubError(
                    "head_sha_mismatch", f"bundle does not contain head {head_sha}"
                )
            _must(
                git_run(["fetch", "--no-tags", "-q", str(bundle), ref], work),
                "checkout_failed",
                "bundle fetch failed (prerequisite commits missing?)",
            )
            proc = git_run(["rev-parse", "FETCH_HEAD"], work)
            _must(proc, "checkout_failed", "bundle FETCH_HEAD did not resolve")
            if proc.stdout.strip() != head_sha:
                raise RemoteGitHubError(
                    "head_sha_mismatch",
                    f"bundle head resolved to {proc.stdout.strip()}, expected {head_sha}",
                )
            pushed = head_sha
        elif kind == "patch":
            _must(
                git_run(["checkout", "-q", "--detach", base_sha], work),
                "checkout_failed",
                f"checkout of base {base_sha} failed",
            )
            patch = work / "payload.diff"
            patch.write_bytes(payload)
            if payload.strip():
                proc = git_run(["apply", "--check", str(patch)], work)
                _must(proc, "artifact_invalid", "patch does not apply on the recorded base")
                _must(
                    git_run(["apply", str(patch)], work),
                    "artifact_invalid",
                    "patch failed to apply",
                )
            _must(git_run(["add", "-A"], work), "checkout_failed", "git add failed")
            proc = git_run(["diff", "--cached", "--quiet"], work)
            if proc.returncode not in (0, 1):
                raise RemoteGitHubError(
                    "checkout_failed", "git diff --cached failed after patch apply"
                )
            if proc.returncode == 1:
                commit = git_run(
                    [
                        "-c",
                        "user.name=sbx-delivery",
                        "-c",
                        "user.email=sbx-delivery@localhost",
                        "commit",
                        "-qm",
                        f"sbx delivery of revision head {head_sha}",
                    ],
                    work,
                )
                _must(commit, "checkout_failed", "failed to commit applied patch")
            proc = git_run(["rev-parse", "HEAD"], work)
            _must(proc, "checkout_failed", "no HEAD after payload apply")
            pushed = proc.stdout.strip()
        else:
            raise RemoteGitHubError("artifact_invalid", f"unknown payload kind {kind!r}")
        _must(
            git_run(["push", "origin", f"{pushed}:refs/heads/{branch}"], work),
            "repo_unavailable",
            f"git push origin {pushed}:refs/heads/{branch} failed",
        )
    remote_sha = ls_remote(repo, f"refs/heads/{branch}", env=env)
    if remote_sha != pushed:
        raise RemoteGitHubError(
            "repo_unavailable",
            f"pushed {branch} but remote resolves to {remote_sha}, expected {pushed}",
        )
    return pushed


class RemoteGitHub:
    """Server-side GitHub REST client for PR status / create / comment / merge.

    ``token=None`` runs unauthenticated — enough for public repos and never
    a silent credential downgrade. Failures raise ``RemoteGitHubError`` with
    the canonical ``repo_unavailable`` code; response bodies are clipped so
    a token-shaped fragment never echoes back.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        api_url: str | None = None,
        client: Any = None,
    ) -> None:
        import httpx

        self._api = (api_url or os.environ.get(API_URL_ENV) or DEFAULT_API_URL).rstrip("/")
        self._token = token
        self._client = client or httpx.Client(timeout=_HTTP_TIMEOUT_S)

    def _request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            resp = self._client.request(method, f"{self._api}{path}", headers=headers, json=body)
        except Exception as exc:
            raise RemoteGitHubError("repo_unavailable", f"GitHub {method} {path} failed: {exc}")
        if not (200 <= resp.status_code < 300):
            detail = ""
            try:
                data = resp.json()
                if isinstance(data, dict):
                    detail = str(data.get("message") or "")
            except Exception:
                detail = ""
            raise RemoteGitHubError(
                "repo_unavailable",
                f"GitHub {method} {path} failed (http {resp.status_code})"
                + (f": {detail[:200]}" if detail else ""),
            )
        try:
            return resp.json()
        except Exception:
            raise RemoteGitHubError("repo_unavailable", f"GitHub {method} {path} returned no JSON")

    def get_pull(self, slug: str, number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{slug}/pulls/{number}")

    def create_pull(
        self,
        slug: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str = "",
        draft: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{slug}/pulls",
            body={"title": title, "head": head, "base": base, "body": body, "draft": draft},
        )

    def merge_pull(self, slug: str, number: int, *, sha: str) -> dict[str, Any]:
        """Merge with GitHub's required head-sha pin — fails closed when the
        PR head moved since ``sha``."""
        return self._request(
            "PUT",
            f"/repos/{slug}/pulls/{number}/merge",
            body={"sha": sha, "merge_method": "merge"},
        )

    def create_comment(self, slug: str, number: int, *, body: str) -> dict[str, Any]:
        """Issue comment — deliberately never a formal ``/reviews`` approval."""
        return self._request("POST", f"/repos/{slug}/issues/{number}/comments", body={"body": body})


__all__ = [
    "API_URL_ENV",
    "DEFAULT_API_URL",
    "RemoteGitHub",
    "RemoteGitHubError",
    "host_git_env",
    "ls_remote",
    "parse_pull_url",
    "push_payload",
    "server_token",
]
