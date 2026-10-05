from pathlib import PurePosixPath

from control.domain.errors import require
from control.domain.events import digest


def subject_manifest(manifest):
    files = []
    seen = set()
    for entry in sorted(manifest["files"], key=lambda f: f["path"]):
        path = PurePosixPath(entry["path"])
        require(
            not path.is_absolute() and ".." not in path.parts and entry["path"] not in seen,
            "capture_failed",
        )
        seen.add(entry["path"])
        files.append({k: entry.get(k) for k in ("path", "type", "mode", "content_digest")})
    return {
        "manifest_version": 1,
        "repository": manifest.get("repository"),
        "namespace": manifest.get("namespace"),
        "base_sha": manifest.get("base_sha"),
        "head_sha": manifest.get("head_sha"),
        "tree_sha": manifest.get("tree_sha"),
        "files": files,
    }


def subject_digest(manifest):
    return digest(subject_manifest(manifest))
