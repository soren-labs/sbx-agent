"""Portable file/native checkpoint. Never captures process memory or auth."""

import base64
import hashlib
import shutil

from protocol.runtime import ProtocolError, digest

from runtime.daemon.files import EXCLUDED
from runtime.security.paths import confined

MAX_SNAPSHOT = 64_000_000


def capture(runtime):
    if runtime.journal.metadata("cleanup_failed") == "true":
        raise ProtocolError("capture_failed")
    if runtime.supervisor.active_operation:
        raise ProtocolError("waiting_capacity")
    files, total = [], 0
    for namespace, root in [
        ("worktree", runtime.worktree),
        ("native", runtime.root / "native-home"),
    ]:
        paths = (
            sorted(root.rglob("*"))
            if namespace == "worktree"
            else sorted((root / ".local/share/opencode").glob("opencode.db*"))
        )
        for path in paths:
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(root)
            if namespace == "worktree" and any(
                p in EXCLUDED - {".git"} or p.startswith(".env") for p in rel.parts
            ):
                continue
            body = path.read_bytes()
            total += len(body)
            if total > MAX_SNAPSHOT:
                raise ProtocolError("quota_exhausted")
            if any(value.encode() in body for value in runtime.supervisor.known_secrets):
                raise ProtocolError("capture_failed")
            files.append(
                {
                    "root": namespace,
                    "path": rel.as_posix(),
                    "mode": path.stat().st_mode & 0o777,
                    "digest": hashlib.sha256(body).hexdigest(),
                    "content": base64.b64encode(body).decode(),
                }
            )
    manifest = {
        "version": 1,
        "session_id": runtime.session_id,
        "generation": int(runtime.journal.metadata("generation")),
        "runtime_version": "1",
        "cli_version": "1.18.29",
        "files": files,
    }
    return {
        "manifest": manifest,
        "content_digest": digest(manifest),
        "watermark": runtime.journal.watermark,
    }


def restore(runtime, manifest):
    if manifest.get("session_id") != runtime.session_id or manifest.get("cli_version") != "1.18.29":
        raise ProtocolError("context_mismatch")
    stage = runtime.root / "restore-stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(mode=0o700)
    try:
        for entry in manifest["files"]:
            namespace = entry["root"]
            if namespace not in {"worktree", "native"}:
                raise ProtocolError("forbidden")
            if namespace == "native" and not entry["path"].startswith(
                ".local/share/opencode/opencode.db"
            ):
                raise ProtocolError("forbidden")
            body = base64.b64decode(entry["content"], validate=True)
            if hashlib.sha256(body).hexdigest() != entry["digest"]:
                raise ProtocolError("capture_failed")
            root = stage / namespace
            root.mkdir(mode=0o700, exist_ok=True)
            path = confined(root, entry["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            path.chmod(entry["mode"] & 0o777)
        # All integrity/path validation finishes before replacing either tree.
        for namespace, destination in [
            ("worktree", runtime.worktree),
            ("native", runtime.root / "native-home"),
        ]:
            source = stage / namespace
            if source.exists():
                if destination.exists():
                    shutil.rmtree(destination)
                source.rename(destination)
        runtime.journal.metadata("generation", manifest["generation"])
        return {"restored": True, "generation": manifest["generation"]}
    finally:
        shutil.rmtree(stage, ignore_errors=True)
