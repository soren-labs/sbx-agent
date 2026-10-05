"""Git transport for Delivery: deterministic commit materialization and CAS push.

Credentials reach git through a private askpass file, never URL/argv/logs.
Plain unconditional force-push is never used.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from control.domain.errors import DomainError

AUTHOR = ("SBX", "sbx@users.noreply.github.com")


class GitTransport:
    def __init__(self, workdir: Path | None = None) -> None:
        self.workdir = Path(workdir or tempfile.gettempdir()) / "sbx-git"
        self.workdir.mkdir(parents=True, exist_ok=True)

    def _env(self, scratch: Path, token: str | None) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(scratch),
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C.UTF-8",
        }
        if token:
            secret = scratch / ".cred"
            fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(token)
            askpass = scratch / "askpass.sh"
            askpass.write_text(
                f'#!/bin/sh\ncase "$1" in\n  Username*) echo x-access-token ;;\n  *) cat "{secret}" ;;\nesac\n'
            )
            os.chmod(askpass, 0o700)
            env["GIT_ASKPASS"] = str(askpass)
        return env

    def _git(
        self, cwd: Path, env: dict[str, str], *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=300
        )
        if check and proc.returncode != 0:
            # stderr may contain the URL but never the token (askpass file only).
            raise DomainError(
                "delivery_unresolved",
                f"git {args[0]} failed",
                details={"stderr": proc.stderr[-300:]},
                retryable=True,
            )
        return proc

    def ls_remote(self, clone_url: str, token: str | None, ref: str) -> str | None:
        scratch = Path(tempfile.mkdtemp(dir=self.workdir))
        try:
            env = self._env(scratch, token)
            out = self._git(scratch, env, "ls-remote", clone_url, ref).stdout.strip()
            return out.split()[0] if out else None
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def materialize_and_push(
        self,
        *,
        clone_url: str,
        token: str | None,
        base_sha: str,
        patch: bytes,
        expected_tree: str,
        message: str,
        timestamp: str,
        ref: str,
        expected_old: str | None,
        push: bool = True,
    ) -> dict[str, Any]:
        """Deterministic commit from pinned baseline + exact tree; push with expected-head CAS."""
        scratch = Path(tempfile.mkdtemp(dir=self.workdir))
        try:
            env = self._env(scratch, token)
            repo = scratch / "repo"
            repo.mkdir()
            self._git(repo, env, "init", "-q")
            self._git(repo, env, "fetch", "-q", "--no-tags", clone_url, base_sha)
            self._git(repo, env, "checkout", "-q", "--detach", base_sha)
            if patch:
                (scratch / "change.patch").write_bytes(patch)
                self._git(repo, env, "apply", "--binary", "--index", str(scratch / "change.patch"))
            tree = self._git(repo, env, "write-tree").stdout.strip()
            if tree != expected_tree:
                raise DomainError(
                    "stale_subject",
                    "materialized tree does not match the sealed ChangeSet",
                    details={"expected_tree": expected_tree, "tree": tree},
                )
            cenv = {
                **env,
                "GIT_AUTHOR_NAME": AUTHOR[0],
                "GIT_AUTHOR_EMAIL": AUTHOR[1],
                "GIT_COMMITTER_NAME": AUTHOR[0],
                "GIT_COMMITTER_EMAIL": AUTHOR[1],
                "GIT_AUTHOR_DATE": timestamp,
                "GIT_COMMITTER_DATE": timestamp,
            }
            commit = self._git(
                repo, cenv, "commit-tree", tree, "-p", base_sha, "-m", message
            ).stdout.strip()
            if push:
                lease = f"--force-with-lease=refs/heads/{ref}:{expected_old or ''}"
                proc = self._git(
                    repo,
                    env,
                    "push",
                    "--porcelain",
                    lease,
                    clone_url,
                    f"{commit}:refs/heads/{ref}",
                    check=False,
                )
                if proc.returncode != 0:
                    raise DomainError(
                        "remote_head_changed",
                        "remote ref changed; expected-head push refused",
                        details={"ref": ref},
                    )
            return {"commit": commit, "tree": tree}
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
