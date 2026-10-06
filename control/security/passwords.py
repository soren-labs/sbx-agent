"""Product password hashing (argon2id) and opaque token hashing."""

from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_HASHER = PasswordHasher()
ALGORITHM = "argon2id"


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        return _HASHER.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    return _HASHER.check_needs_rehash(stored)


def new_token(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Stored verifier for high-entropy tokens/keys (never the plaintext)."""
    return hashlib.sha256(token.encode()).hexdigest()


# Constant work for unknown accounts to avoid user-enumeration timing.
DUMMY_HASH = _HASHER.hash("sbx-dummy-password-never-used")
