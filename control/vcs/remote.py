"""VcsRemote — the provider boundary for exact-subject git effects
(RFC 167 §05).

Implementations perform remote effects with compare-and-swap preconditions:
a push pins an expected-old SHA, a PR create first discovers an existing
association, a merge enforces the pinned head. Ambiguity MUST surface as an
error, never as a speculative repeat effect.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Protocol


class RemoteConflict(Exception):
    """Remote precondition failed — expected old/head drifted."""


class RemoteError(Exception):
    """Transport/provider failure with an ambiguous or retryable outcome."""


def _git(
    args: list[str], *, cwd: Path | None = None, env: dict | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    full_env = dict(env or {})
    full_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    cp = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env={**os.environ, **full_env},
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check and cp.returncode != 0:
        raise RemoteError(f"git {' '.join(args[:2])} failed: {cp.stderr.strip()[:400]}")
    return cp


class VcsRemote(Protocol):
    def ls_remote(self, repository: str, ref: str) -> str | None:
        """Current SHA of ``ref`` on the remote, or None if absent."""

    def push(
        self,
        repository: str,
        local_commit: str,
        ref: str,
        expected_old: str | None,
        scratch: Path,
    ) -> dict:
        """CAS-push ``local_commit`` to ``refs/heads/<ref>``.

        ``expected_old=None`` means the ref must not already exist.
        Returns ``{"head_sha": ..., "ref": ...}``."""

    def find_pull_request(self, repository: str, *, head_ref: str, base_ref: str) -> dict | None:
        """Existing open PR association ``{number, url, head_sha}`` or None."""

    def create_pull_request(
        self,
        repository: str,
        *,
        head_ref: str,
        base_ref: str,
        title: str,
        body: str,
        draft: bool,
    ) -> dict:
        """Create PR; returns ``{number, url, head_sha, state, draft}``."""

    def pull_request_state(self, repository: str, number: int) -> dict:
        """``{state, draft, head_sha, mergeable, merged}``."""

    def checks(self, repository: str, head_sha: str) -> list[dict]:
        """CI check runs ``[{name, status, conclusion}]`` for the head."""

    def merge_pull_request(
        self,
        repository: str,
        number: int,
        *,
        method: str,
        expected_head: str,
    ) -> dict:
        """Merge with expected-head precondition; returns verified evidence."""


def materialize_commit(
    *,
    files: list[dict],
    read_blob,  # callable(storage_key) -> bytes
    base_commit: str | None,
    repository_url: str | None,
    message: str,
    scratch: Path,
    author: dict | None = None,
) -> str:
    """Deterministically map a patch-only ChangeSet onto a commit.

    Identical (files, base, message) always yields the same SHA: timestamps
    are pinned to the unix epoch, author/committer metadata is fixed.
    """
    env = {
        "GIT_AUTHOR_NAME": (author or {}).get("name", "sbx"),
        "GIT_AUTHOR_EMAIL": (author or {}).get("email", "sbx@localhost"),
        "GIT_COMMITTER_NAME": (author or {}).get("name", "sbx"),
        "GIT_COMMITTER_EMAIL": (author or {}).get("email", "sbx@localhost"),
        "GIT_AUTHOR_DATE": "@0 +0000",
        "GIT_COMMITTER_DATE": "@0 +0000",
    }
    work = scratch / "mat"
    if work.exists():
        import shutil

        shutil.rmtree(work)
    work.mkdir(parents=True)
    _git(["init", "-q", "--initial-branch", "tmp"], cwd=work)
    if base_commit and repository_url:
        _git(
            ["fetch", "-q", "--depth", "1", repository_url, base_commit],
            cwd=work,
        )
        _git(["checkout", "-q", "FETCH_HEAD"], cwd=work)
    for f in files:
        if f["file_type"] == "deleted":
            p = work / f["path"]
            if p.exists():
                p.unlink()
            continue
        target = work / f["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if f["file_type"] == "symlink":
            target.symlink_to(f["symlink_target"])
        elif f["blob_id"]:
            target.write_bytes(read_blob(f["storage_key"]))
            target.chmod(int(f["mode"]))
    _git(["add", "-A"], cwd=work)
    tree = _git(["write-tree"], cwd=work).stdout.strip()
    args = ["commit-tree", tree, "-m", message]
    if base_commit:
        args.extend(["-p", base_commit])
    return _git(args, cwd=work, env=env).stdout.strip()


class GitCliRemote:
    """Plain-git transport over an arbitrary remote URL. PR capabilities
    are provider-specific — they raise UnsupportedProvider on this remote."""

    def __init__(self, remote_url: str, *, auth_env: dict | None = None) -> None:
        self.remote_url = remote_url
        self.auth_env = auth_env or {}

    def ls_remote(self, repository: str, ref: str) -> str | None:
        cp = _git(
            ["ls-remote", self.remote_url, f"refs/heads/{ref}"],
            env=self.auth_env,
        )
        for line in cp.stdout.splitlines():
            sha, _, name = line.partition("\t")
            if name.strip() == f"refs/heads/{ref}":
                return sha.strip()
        return None

    def push(self, repository, local_commit, ref, expected_old, scratch: Path) -> dict:
        current = self.ls_remote(repository, ref)
        if expected_old is None:
            if current is not None:
                raise RemoteConflict(f"refs/heads/{ref} already exists at {current}")
        elif current != expected_old:
            raise RemoteConflict(
                f"refs/heads/{ref} drifted: expected {expected_old}, saw {current}"
            )
        work = scratch / "push"
        work.mkdir(parents=True, exist_ok=True)
        spec = f"{local_commit}:refs/heads/{ref}"
        args = ["push", self.remote_url, spec]
        if expected_old is not None:
            args = [
                "push",
                f"--force-with-lease=refs/heads/{ref}:{expected_old}",
                self.remote_url,
                spec,
            ]
        _git(args, cwd=work, env=self.auth_env)
        # Verified success evidence: the remote now advertises the intended SHA.
        seen = self.ls_remote(repository, ref)
        if seen != local_commit:
            raise RemoteError(f"post-push ls-remote shows {seen}, expected {local_commit}")
        return {"head_sha": local_commit, "ref": f"refs/heads/{ref}"}

    def _pr_unsupported(self, *_a, **_k):
        raise UnsupportedProvider("git transport cannot manage pull requests")

    find_pull_request = _pr_unsupported
    create_pull_request = _pr_unsupported
    pull_request_state = _pr_unsupported
    checks = _pr_unsupported
    merge_pull_request = _pr_unsupported


class UnsupportedProvider(RemoteError):
    pass


class GithubRemote(GitCliRemote):
    """GitHub remote: git CLI for exact-subject push, REST for PR + checks.

    The token is used only inside this process for auth headers — never
    logged, stored, or returned in evidence.
    """

    def __init__(self, token: str, *, api_base: str = "https://api.github.com") -> None:
        super().__init__("")
        self.token = token
        self.api_base = api_base.rstrip("/")

    def _request(
        self, method: str, path: str, payload: dict | None = None, allow_404: bool = False
    ) -> dict | list | None:
        import httpx

        url = f"{self.api_base}{path}"
        resp = httpx.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=payload,
            timeout=30,
        )
        if allow_404 and resp.status_code == 404:
            return None
        if resp.status_code == 422:
            raise RemoteConflict(resp.text[:400])
        if resp.status_code >= 400:
            raise RemoteError(f"github {method} {path}: {resp.status_code} {resp.text[:300]}")
        return resp.json() if resp.content else {}

    def _remote_url(self, repository: str) -> str:
        # Token is embedded in-process for git; never returned in evidence.
        return f"https://x-access-token:{self.token}@github.com/{repository}.git"

    def ls_remote(self, repository: str, ref: str) -> str | None:
        data = self._request("GET", f"/repos/{repository}/git/ref/heads/{ref}", allow_404=True)
        if not data:
            return None
        return (data.get("object") or {}).get("sha")

    def push(self, repository, local_commit, ref, expected_old, scratch: Path) -> dict:
        current = self.ls_remote(repository, ref)
        if expected_old is None:
            if current is not None:
                raise RemoteConflict(f"refs/heads/{ref} exists at {current}")
        elif current != expected_old:
            raise RemoteConflict(
                f"refs/heads/{ref} drifted: expected {expected_old}, saw {current}"
            )
        self.remote_url = self._remote_url(repository)
        return super().push(repository, local_commit, ref, expected_old, scratch)

    def find_pull_request(self, repository, *, head_ref, base_ref) -> dict | None:
        data = self._request(
            "GET",
            f"/repos/{repository}/pulls?head={head_ref}&base={base_ref}&state=open",
        )
        if not data:
            return None
        pr = data[0]
        return {
            "number": pr["number"],
            "url": pr["html_url"],
            "head_sha": pr["head"]["sha"],
            "state": pr["state"],
            "draft": pr.get("draft", False),
        }

    def create_pull_request(self, repository, *, head_ref, base_ref, title, body, draft) -> dict:
        pr = self._request(
            "POST",
            f"/repos/{repository}/pulls",
            {"head": head_ref, "base": base_ref, "title": title, "body": body, "draft": draft},
        )
        return {
            "number": pr["number"],
            "url": pr["html_url"],
            "head_sha": pr["head"]["sha"],
            "state": pr["state"],
            "draft": pr.get("draft", False),
        }

    def pull_request_state(self, repository: str, number: int) -> dict:
        pr = self._request("GET", f"/repos/{repository}/pulls/{number}")
        return {
            "state": pr["state"],
            "draft": pr.get("draft", False),
            "head_sha": pr["head"]["sha"],
            "mergeable": pr.get("mergeable"),
            "merged": pr.get("merged", False),
            "merge_commit_sha": pr.get("merge_commit_sha"),
        }

    def checks(self, repository: str, head_sha: str) -> list[dict]:
        data = self._request("GET", f"/repos/{repository}/commits/{head_sha}/check-runs") or {}
        return [
            {
                "name": c["name"],
                "status": c["status"],
                "conclusion": c.get("conclusion"),
            }
            for c in (data.get("check_runs") or [])
        ]

    def merge_pull_request(self, repository, number, *, method, expected_head) -> dict:
        try:
            data = self._request(
                "PUT",
                f"/repos/{repository}/pulls/{number}/merge",
                {"merge_method": method, "sha": expected_head},
            )
        except RemoteConflict as exc:
            # Expected-head precondition refused or 405/409 drift — unresolved.
            raise RemoteConflict(str(exc)) from exc
        merged = self.pull_request_state(repository, number)
        if not merged.get("merged"):
            raise RemoteError("merge call returned but PR is not merged")
        return {
            "merged": True,
            "merge_commit_sha": data.get("sha") or merged.get("merge_commit_sha"),
            "head_sha": expected_head,
        }


class FakeRemote:
    """Deterministic in-memory remote for tests — same CAS/ambiguity
    semantics as the real providers, no network."""

    def __init__(self) -> None:
        self.refs: dict[str, dict[str, str]] = {}
        self.prs: dict[tuple[str, int], dict] = {}
        self.next_pr = 1
        self.check_results: dict[str, list[dict]] = {}
        self.log: list[tuple] = []

    def _refkey(self, repository: str, ref: str) -> str:
        return f"{repository}@{ref}"

    def ls_remote(self, repository: str, ref: str) -> str | None:
        return self.refs.get(self._refkey(repository, ref), {}).get("sha")

    def push(self, repository, local_commit, ref, expected_old, scratch: Path) -> dict:
        key = self._refkey(repository, ref)
        current = self.refs.get(key, {}).get("sha")
        if expected_old is None:
            if current is not None:
                raise RemoteConflict(f"{key} exists at {current}")
        elif current != expected_old:
            raise RemoteConflict(f"{key} drifted: expected {expected_old}, saw {current}")
        self.refs[key] = {"sha": local_commit}
        self.log.append(("push", repository, ref, local_commit))
        return {"head_sha": local_commit, "ref": f"refs/heads/{ref}"}

    def find_pull_request(self, repository, *, head_ref, base_ref) -> dict | None:
        for (repo, num), pr in self.prs.items():
            if repo == repository and pr["head_ref"] == head_ref and pr["state"] == "open":
                return {
                    "number": num,
                    "url": pr["url"],
                    "head_sha": pr["head_sha"],
                    "state": pr["state"],
                    "draft": pr["draft"],
                }
        return None

    def create_pull_request(self, repository, *, head_ref, base_ref, title, body, draft) -> dict:
        num = self.next_pr
        self.next_pr += 1
        head_sha = self.refs.get(self._refkey(repository, head_ref), {}).get("sha")
        pr = {
            "number": num,
            "url": f"https://fake.local/{repository}/pull/{num}",
            "head_ref": head_ref,
            "base_ref": base_ref,
            "head_sha": head_sha,
            "state": "open",
            "draft": draft,
            "merged": False,
        }
        self.prs[(repository, num)] = pr
        self.log.append(("pr", repository, head_ref, num))
        return {
            "number": num,
            "url": pr["url"],
            "head_sha": head_sha,
            "state": "open",
            "draft": draft,
        }

    def pull_request_state(self, repository: str, number: int) -> dict:
        pr = self.prs[(repository, number)]
        head_sha = self.refs.get(self._refkey(repository, pr["head_ref"]), {}).get("sha")
        return {
            "state": pr["state"],
            "draft": pr["draft"],
            "head_sha": head_sha,
            "mergeable": True,
            "merged": pr["merged"],
            "merge_commit_sha": pr.get("merge_commit_sha"),
        }

    def checks(self, repository: str, head_sha: str) -> list[dict]:
        return list(self.check_results.get(head_sha, []))

    def merge_pull_request(self, repository, number, *, method, expected_head) -> dict:
        pr = self.prs[(repository, number)]
        head_sha = self.refs.get(self._refkey(repository, pr["head_ref"]), {}).get("sha")
        if head_sha != expected_head:
            raise RemoteConflict(f"pr head drifted: expected {expected_head}, saw {head_sha}")
        pr["merged"] = True
        pr["state"] = "closed"
        pr["merge_commit_sha"] = f"merge-{number}-{expected_head[:8]}"
        self.log.append(("merge", repository, number, expected_head))
        return {
            "merged": True,
            "merge_commit_sha": pr["merge_commit_sha"],
            "head_sha": expected_head,
        }
