"""One artifact-secret policy for every surface that copies Worktree bytes out of, or
back into, the runtime: ChangeSet capture, checkpoints, restore and file reads.

* Credential-bearing *paths* (``.env``, ``auth.json``, private keys, ...) are never
  captured, checkpointed, restored or read. Safe templates (``.env.example``) are
  ordinary files.
* Credential *values* are refused/excluded by content: the exact credentials the
  runtime was handed for this lease (``known``) and obvious token/private-key
  patterns. Free-form native state (session transcripts) is checked only for the
  exact known values, because transcripts can legitimately mention token shapes.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Iterable
from pathlib import PurePosixPath

# Directory names anywhere in a path that only ever hold credentials or runtime state.
SECRET_DIRS = frozenset({".sbx", ".ssh", ".aws", ".gnupg", ".docker", ".kube"})
# Exact file names (case-insensitive) that carry credentials.
SECRET_NAMES = frozenset(
    {
        ".env",
        "auth.json",
        ".netrc",
        ".git-credentials",
        ".npmrc",
        ".pypirc",
        ".modal.toml",
        "credentials.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
# File-name globs (case-insensitive) that carry credentials.
SECRET_GLOBS = (".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks")
# Templates are documentation, not credentials, and remain ordinary files.
TEMPLATE_NAMES = frozenset(
    {".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults"}
)

_PATTERNS = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    rb"|gh[pousr]_[A-Za-z0-9]{30,}"
    rb"|github_pat_[A-Za-z0-9_]{30,}"
    rb"|sk-[A-Za-z0-9_-]{32,}"
    rb"|\bAKIA[0-9A-Z]{16}\b"
    rb"|\bxox[abprs]-[A-Za-z0-9-]{20,}"
)
MIN_KNOWN = 8  # shorter "secrets" would match ordinary bytes


def is_template(name: str) -> bool:
    return name.lower() in TEMPLATE_NAMES


def is_secret_path(rel: str) -> bool:
    """True for root-relative paths that must never leave or enter the runtime."""
    parts = PurePosixPath(rel.replace("\\", "/")).parts
    if not parts:
        return False
    if any(p.lower() in SECRET_DIRS for p in parts[:-1]):
        return True
    name = parts[-1].lower()
    if name in SECRET_DIRS:
        return True
    if is_template(name):
        return False
    return name in SECRET_NAMES or any(fnmatch.fnmatchcase(name, g) for g in SECRET_GLOBS)


def known_values(known: Iterable[str]) -> list[bytes]:
    return sorted(
        {k.encode() for k in known if isinstance(k, str) and len(k) >= MIN_KNOWN},
        key=len,
        reverse=True,
    )


def secret_reason(data: bytes, known: Iterable[str] = (), *, patterns: bool = True) -> str | None:
    """Why ``data`` must not be copied as an artifact (never the secret itself)."""
    if any(k in data for k in known_values(known)):
        return "known_credential"
    if patterns and _PATTERNS.search(data):
        return "secret_pattern"
    return None


def redact_bytes(data: bytes, known: Iterable[str] = ()) -> bytes:
    for value in known_values(known):
        data = data.replace(value, b"REDACTED")
    return _PATTERNS.sub(b"REDACTED", data)


def collect_known(value: object) -> set[str]:
    """Every string leaf of a secrets bundle (the values the runtime may materialize)."""
    if isinstance(value, str):
        values = {value} if len(value) >= MIN_KNOWN else set()
        # Uploaded auth_json is a string on the wire; its token leaves are what
        # the CLI may copy into files or output.
        try:
            parsed = json.loads(value)
        except ValueError:
            return values
        return values | collect_known(parsed) if isinstance(parsed, dict | list) else values
    if isinstance(value, dict):
        return {s for v in value.values() for s in collect_known(v)}
    if isinstance(value, list | tuple):
        return {s for v in value for s in collect_known(v)}
    return set()


def git_excludes() -> tuple[str, ...]:
    """Pathspecs that keep credential paths out of a capture index (templates re-added)."""
    names = [f":(exclude,glob,icase)**/{n}" for n in sorted(SECRET_NAMES)]
    globs = [f":(exclude,glob,icase)**/{g}" for g in SECRET_GLOBS]
    dirs = [f":(exclude,glob,icase)**/{d}/**" for d in sorted(SECRET_DIRS)]
    return (*names, *globs, *dirs)


def git_templates() -> tuple[str, ...]:
    return tuple(f":(glob,icase)**/{n}" for n in sorted(TEMPLATE_NAMES))
