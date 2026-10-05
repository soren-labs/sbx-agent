"""Local bare Git repository + fake pull-request host backed by that repository."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from control.domain.errors import DomainError


def git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(cwd),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def make_repo(tmp: Path) -> tuple[Path, str]:
    bare = tmp / "remote" / "demo.git"
    bare.mkdir(parents=True)
    git(bare, "init", "-q", "--bare", "-b", "main")
    seed = tmp / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("# demo\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "init")
    git(seed, "push", "-q", str(bare), "main")
    return bare, f"file://{bare}"


class FakeHost:
    def __init__(self, bare: Path) -> None:
        self.bare = bare
        self.prs: dict[int, dict[str, Any]] = {}
        self.check_runs: dict[str, list[dict[str, Any]]] = {}
        self.merges: list[dict[str, Any]] = []

    def _head(self, ref: str) -> str | None:
        try:
            return git(self.bare, "rev-parse", f"refs/heads/{ref}")
        except subprocess.CalledProcessError:
            return None

    def _refresh(self, pr: dict[str, Any]) -> dict[str, Any]:
        pr["head_sha"] = self._head(pr["head_ref"]) or pr["head_sha"]
        return dict(pr)

    def find_pr(self, repo: str, token: str, head_ref: str) -> dict[str, Any] | None:
        for pr in self.prs.values():
            if pr["head_ref"] == head_ref:
                return self._refresh(pr)
        return None

    def create_pr(
        self, repo: str, token: str, *, head_ref: str, base: str, title: str, body: str, draft: bool
    ) -> dict[str, Any]:
        number = len(self.prs) + 1
        self.prs[number] = {
            "number": number,
            "url": f"https://github.test/{repo}/pull/{number}",
            "node_id": f"PR_{number}",
            "head_sha": self._head(head_ref),
            "head_ref": head_ref,
            "base_ref": base,
            "state": "open",
            "draft": draft,
            "mergeable": True,
            "body": body,
        }
        return dict(self.prs[number])

    def get_pr(self, repo: str, token: str, number: int) -> dict[str, Any]:
        return self._refresh(self.prs[number])

    def checks(self, repo: str, token: str, sha: str) -> list[dict[str, Any]]:
        return self.check_runs.get(sha, [])

    def mark_ready(self, repo: str, token: str, pr: dict[str, Any]) -> None:
        self.prs[pr["number"]]["draft"] = False

    def merge(self, repo: str, token: str, number: int, *, sha: str, method: str) -> dict[str, Any]:
        pr = self._refresh(self.prs[number])
        if pr["head_sha"] != sha:
            raise DomainError("remote_head_changed", "head changed")
        if self.prs[number]["draft"]:
            raise DomainError("gate_blocked", "draft")
        git(self.bare, "update-ref", f"refs/heads/{pr['base_ref']}", sha)
        self.prs[number]["state"] = "merged"
        self.merges.append({"number": number, "sha": sha, "method": method})
        return {"merged": True, "sha": sha}
