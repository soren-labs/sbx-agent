import base64
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from protocol.runtime import ProtocolError, digest

from runtime.daemon.files import EXCLUDED
from runtime.security.paths import confined


def git(root, *argv):
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
    }
    result = subprocess.run(
        ["git", "-C", str(root), *argv], env=env, capture_output=True, timeout=30
    )
    if result.returncode:
        raise ProtocolError("capture_failed")
    return result.stdout


def capture(runtime, payload):
    generation = int(runtime.journal.metadata("generation"))
    if payload["generation"] != generation:
        raise ProtocolError("version_conflict")
    files, contents = [], {}
    git_repo = (runtime.worktree / ".git").exists()
    if git_repo:
        names = (
            git(runtime.worktree, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
            .decode()
            .split("\x00")
        )
        baseline = set(
            git(runtime.worktree, "ls-tree", "-r", "--name-only", "-z", payload["base_sha"])
            .decode()
            .split("\x00")
        )
    else:
        names = [
            p.relative_to(runtime.worktree).as_posix()
            for p in runtime.worktree.rglob("*")
            if p.is_file()
        ]
        baseline = set()
    selected = set(filter(None, names))
    total = 0
    for name in sorted(selected | baseline):
        if not name:
            continue
        rel = Path(name)
        if any(p in EXCLUDED or p.startswith(".env") for p in rel.parts):
            if git_repo and git(runtime.worktree, "diff", payload["base_sha"], "--", name):
                raise ProtocolError("capture_failed")
            continue
        path = runtime.worktree / name
        if path.is_symlink():
            if not path.resolve().is_relative_to(runtime.worktree.resolve()):
                raise ProtocolError("capture_failed")
            content = os.readlink(path).encode()
            mode, kind = "120000", "symlink"
        elif path.is_file():
            path = confined(runtime.worktree, name)
            content = path.read_bytes()
            mode, kind = ("100755" if path.stat().st_mode & 0o111 else "100644"), "file"
        elif name in baseline:
            files.append(
                {"path": name, "mode": "100644", "type": "deleted", "content_digest": None}
            )
            continue
        else:
            continue
        total += len(content)
        if total > 64_000_000 or len(files) > 2000:
            raise ProtocolError("quota_exhausted")
        if any(value.encode() in content for value in runtime.supervisor.known_secrets):
            raise ProtocolError("capture_failed")
        sha = hashlib.sha256(content).hexdigest()
        files.append({"path": name, "mode": mode, "type": kind, "content_digest": sha})
        contents[name] = base64.b64encode(content).decode()
    # A dirty capture is patch-only; a clean committed subject pins exact HEAD/tree.
    head, tree = None, None
    if git_repo and not git(runtime.worktree, "status", "--porcelain"):
        head = git(runtime.worktree, "rev-parse", "HEAD").decode().strip()
        tree = git(runtime.worktree, "rev-parse", "HEAD^{tree}").decode().strip()
    manifest = {
        "manifest_version": 1,
        "repository": payload.get("repository"),
        "namespace": None if payload.get("repository") else runtime.session_id,
        "base_sha": payload.get("base_sha"),
        "head_sha": head,
        "tree_sha": tree,
        "files": files,
    }
    return {
        "subject": manifest,
        "subject_digest": digest(manifest),
        "generation": generation,
        "contents": contents,
    }


def apply(runtime, payload):
    if payload["generation"] != int(runtime.journal.metadata("generation")):
        raise ProtocolError("version_conflict")
    subject = payload["subject"]
    if payload["subject_digest"] != digest(subject):
        raise ProtocolError("stale_subject")
    if (
        subject["base_sha"]
        and git(runtime.worktree, "rev-parse", "HEAD").decode().strip() != subject["base_sha"]
    ):
        raise ProtocolError("stale_subject")
    stage = runtime.root / "apply-stage"
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(runtime.worktree, stage, symlinks=True)
    try:
        for entry in subject["files"]:
            path = confined(stage, entry["path"])
            if entry["type"] == "deleted":
                path.unlink(missing_ok=True)
                continue
            content = base64.b64decode(payload["contents"][entry["path"]], validate=True)
            if hashlib.sha256(content).hexdigest() != entry["content_digest"]:
                raise ProtocolError("capture_failed")
            path.parent.mkdir(parents=True, exist_ok=True)
            if entry["type"] == "symlink":
                target = content.decode()
                if not (path.parent / target).resolve().is_relative_to(stage.resolve()):
                    raise ProtocolError("forbidden")
                path.unlink(missing_ok=True)
                path.symlink_to(target)
            else:
                path.write_bytes(content)
                path.chmod(0o755 if entry["mode"] == "100755" else 0o644)
        backup = runtime.root / "apply-backup"
        shutil.rmtree(backup, ignore_errors=True)
        runtime.worktree.rename(backup)
        stage.rename(runtime.worktree)
        shutil.rmtree(backup)
        generation = payload["generation"] + 1
        runtime.journal.metadata("generation", generation)
        return {"applied": True, "generation": generation}
    finally:
        shutil.rmtree(stage, ignore_errors=True)
