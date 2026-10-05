"""Logical Worktree realization: restore, observe, checkpoint export, files."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

from runtime.security.credentials import scrub, write_secret_file
from runtime.security.paths import PathEscape, safe_join

MAX_READ = 1_000_000
MAX_CHECKPOINT = 200 * 1024 * 1024
GIT_ENV_BASE = {"GIT_TERMINAL_PROMPT": "0", "LANG": "C.UTF-8"}


class WorktreeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def git(
    cwd: Path, *args: str, env: dict[str, str] | None = None, check: bool = True, timeout: int = 600
) -> subprocess.CompletedProcess[str]:
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd), **GIT_ENV_BASE}
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise WorktreeError("git_failed", f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
    return proc


class Worktree:
    def __init__(self, root: Path, state_dir: Path) -> None:
        self.root = root
        self.path = root / "worktree"
        self.state_dir = state_dir
        self.generation = 0

    def _askpass(self, credential: dict[str, Any] | None) -> tuple[dict[str, str], list[Path]]:
        """Purpose-bound Git credential helper: never in URL, argv or logs."""
        if not credential or not credential.get("password"):
            return {}, []
        secret = write_secret_file(self.state_dir / "git" / "credential", credential["password"])
        user = write_secret_file(
            self.state_dir / "git" / "username", credential.get("username") or "x-access-token"
        )
        script = self.state_dir / "git" / "askpass.sh"
        script.write_text(
            f'#!/bin/sh\ncase "$1" in\n  Username*) cat "{user}" ;;\n  *) cat "{secret}" ;;\nesac\n'
        )
        os.chmod(script, 0o700)
        return {"GIT_ASKPASS": str(script)}, [secret, user, script]

    def restore(self, payload: dict[str, Any], credential: dict[str, Any] | None) -> dict[str, Any]:
        if self.path.exists() and payload.get("replace") is not True and any(self.path.iterdir()):
            raise WorktreeError("worktree_live", "worktree already realized on this lease")
        if self.path.exists():
            shutil.rmtree(self.path)
        checkpoint = payload.get("checkpoint_b64")
        native = None
        if checkpoint:
            native = self._extract(base64.b64decode(checkpoint))
        elif payload.get("repository"):
            repo = payload["repository"]
            env, files = self._askpass(credential)
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                git(self.root, "clone", "--no-tags", repo["clone_url"], str(self.path), env=env)
                target = repo.get("base_sha") or repo.get("base_ref")
                if target:
                    git(self.path, "fetch", "--no-tags", "origin", target, env=env, check=False)
                    git(
                        self.path,
                        "checkout",
                        "--detach",
                        repo.get("base_sha") or f"origin/{repo['base_ref']}",
                    )
            finally:
                scrub(files)
            git(self.path, "remote", "set-url", "origin", repo["clone_url"])
        else:
            self.path.mkdir(parents=True)
            git(self.path, "init", "-q")
            git(
                self.path,
                "-c",
                "user.name=sbx",
                "-c",
                "user.email=sbx@localhost",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "baseline",
            )
        base_sha = git(self.path, "rev-parse", "HEAD").stdout.strip()
        self.generation = int(payload.get("generation") or 0)
        return {
            "base_sha": base_sha,
            "generation": self.generation,
            "restored_from": "checkpoint"
            if checkpoint
            else ("repository" if payload.get("repository") else "empty"),
            "native_state": native,
        }

    def observe(self) -> dict[str, Any]:
        if not self.path.exists():
            raise WorktreeError("executor_unavailable", "worktree not realized")
        status = git(self.path, "status", "--porcelain=v1", "--untracked-files=all").stdout
        files = []
        for line in status.splitlines():
            code, path = line[:2], line[3:]
            files.append({"path": path, "status": code.strip() or "?"})
        return {
            "generation": self.generation,
            "head": git(self.path, "rev-parse", "HEAD").stdout.strip(),
            "files": files,
        }

    def bump(self) -> int:
        self.generation += 1
        return self.generation

    # -- files ----------------------------------------------------------------------
    def list(self, rel: str = "") -> list[dict[str, Any]]:
        base = safe_join(self.path, rel) if rel else self.path
        if not base.is_dir():
            raise WorktreeError("not_found", "not a directory")
        out = []
        for entry in sorted(base.iterdir()):
            if entry.name == ".git":
                continue
            out.append(
                {
                    "path": str(entry.relative_to(self.path)),
                    "type": "dir" if entry.is_dir() else "file",
                    "size": entry.stat().st_size if entry.is_file() else None,
                }
            )
        return out[:1000]

    def read(self, rel: str) -> dict[str, Any]:
        target = safe_join(self.path, rel)
        if not target.is_file():
            raise WorktreeError("not_found", "file not found")
        data = target.read_bytes()[:MAX_READ]
        try:
            return {
                "path": rel,
                "encoding": "utf-8",
                "content": data.decode("utf-8"),
                "truncated": target.stat().st_size > MAX_READ,
                "digest": "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        except UnicodeDecodeError:
            return {
                "path": rel,
                "encoding": "base64",
                "content": base64.b64encode(data).decode(),
                "truncated": target.stat().st_size > MAX_READ,
            }

    def write(self, rel: str, content: str, expected_digest: str | None) -> dict[str, Any]:
        target = safe_join(self.path, rel)
        if expected_digest is not None:
            current = (
                "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
                if target.exists()
                else "absent"
            )
            if current != expected_digest:
                raise WorktreeError("version_conflict", "file content changed since it was read")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return {
            "path": rel,
            "generation": self.bump(),
            "digest": "sha256:" + hashlib.sha256(content.encode()).hexdigest(),
        }

    # -- checkpoint ----------------------------------------------------------------
    def checkpoint(self, native: dict[str, list[Path]]) -> dict[str, Any]:
        """Secret-free tar of the Worktree plus approved native state (no credentials)."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(self.path, arcname="worktree")
            for session_home, paths in native.items():
                for p in paths:
                    tar.add(p, arcname=f"native/{session_home}/{p.name}")
        data = buf.getvalue()
        if len(data) > MAX_CHECKPOINT:
            raise WorktreeError("capture_failed", "checkpoint exceeds size limit")
        return {
            "checkpoint_b64": base64.b64encode(data).decode(),
            "content_digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "generation": self.generation,
        }

    def _extract(self, data: bytes) -> dict[str, list[str]]:
        native: dict[str, list[str]] = {}
        staging = self.root / ".restore"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.issym() or member.islnk():
                    try:
                        safe_join(
                            staging, os.path.join(os.path.dirname(member.name), member.linkname)
                        )
                    except PathEscape as exc:
                        raise WorktreeError("capture_failed", "archive link escapes root") from exc
                safe_join(staging, member.name)
            tar.extractall(staging, filter="tar")
        shutil.move(str(staging / "worktree"), str(self.path))
        native_root = staging / "native"
        if native_root.exists():
            for session_dir in native_root.iterdir():
                native[session_dir.name] = [str(p) for p in session_dir.iterdir()]
        self._native_staging = native_root
        return native
