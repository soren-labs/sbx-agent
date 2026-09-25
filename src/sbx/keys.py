"""Bootstrap ``sbx_<key>`` handling (SOR-98).

The plaintext token is generated locally, written once to
``<state>/bootstrap.key`` (mode 0600) and injected into the control plane
through the ``sbx-v1-bootstrap`` Modal Secret. Control stores only
``sha256(token)`` (``InMemoryApiKeyStore.seed``); nothing here ever logs or
returns more than the hash — :func:`fingerprint` is what doctor prints.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Mapping
from pathlib import Path

from sbx.config import API_KEY_ENV, key_path

KEY_PREFIX = "sbx_"
KEY_BYTES = 20


def generate_key() -> str:
    """Mint a fresh ``sbx_`` token (40 hex chars after the prefix)."""
    return f"{KEY_PREFIX}{secrets.token_hex(KEY_BYTES)}"


def valid_key(token: str) -> bool:
    return isinstance(token, str) and token.startswith(KEY_PREFIX) and len(token) > len(KEY_PREFIX)


def key_hash(token: str) -> str:
    """The sha256 the control plane stores — the only durable form."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def fingerprint(token: str) -> str:
    """Short non-secret identifier for doctor/status output."""
    return f"sha256:{key_hash(token)[:12]}"


def load_or_create_key(path: Path) -> tuple[str, bool]:
    """Return ``(token, created)``. Creates a 0600 file atomically.

    A file whose contents are not a valid key is treated as absent and
    overwritten — its mode is still forced to 0600.
    """
    token = read_key(path)
    if token is not None:
        return token, False
    path.parent.mkdir(parents=True, exist_ok=True)
    token = generate_key()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token, True


def read_key(path: Path) -> str | None:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token if valid_key(token) else None


def resolve_api_key(env: Mapping[str, str] | None = None) -> str | None:
    """Env ``SBX_API_KEY`` wins over the state-dir key file."""
    env = os.environ if env is None else env
    token = (env.get(API_KEY_ENV) or "").strip()
    if token:
        return token
    return read_key(key_path(env))
