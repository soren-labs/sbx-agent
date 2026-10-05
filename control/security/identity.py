import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


def token():
    return secrets.token_urlsafe(32)


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(value):
    if not isinstance(value, str) or len(value) < 8 or len(value) > 1024:
        raise ValueError("Password must be 8–1024 characters")
    return HASHER.hash(value)


def password_matches(value, encoded):
    try:
        return HASHER.verify(encoded, value)
    except (VerificationError, InvalidHashError):
        return False
