"""Authenticated envelope encryption for credential payloads (RFC 167 §06).

The master keyring lives outside the DB (env or injected bytes); ciphertext,
key id and nonce are what persist. AAD binds workspace / connection /
credential-version / format so a ciphertext can never be replayed under a
different owner context. Bounded rotation: ``keyring`` maps key_id -> 32-byte
AES key; sealing always uses ``active_key_id`` while opening accepts any
known key.
"""

from __future__ import annotations

import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from control.domain.errors import DomainError

_NONCE_BYTES = 12


class VaultError(DomainError):
    def __init__(self, message: str):
        super().__init__("credential_invalid", message)


class Vault:
    """AES-256-GCM envelope sealer/opener.

    keyring: {key_id: raw 32-byte key}. ``active_key_id`` selects the sealing
    key; opening any known key id works (bounded rotation window).
    """

    def __init__(self, keyring: dict[str, bytes], active_key_id: str):
        if active_key_id not in keyring:
            raise VaultError(f"active key {active_key_id!r} not in keyring")
        for key_id, key in keyring.items():
            if len(key) not in (16, 24, 32):
                raise VaultError(f"key {key_id!r} has invalid length {len(key)}")
        self._keyring = dict(keyring)
        self._active = active_key_id

    @classmethod
    def generate(cls, key_id: str = "k1") -> Vault:
        """Fresh single-key vault for tests/development."""
        return cls({key_id: AESGCM.generate_key(bit_length=256)}, key_id)

    @classmethod
    def from_env(cls, var: str = "SBX_VAULT_KEYS") -> Vault:
        """Keyring from env: JSON {key_id: base64-key}; first key is active
        unless ``SBX_VAULT_ACTIVE_KEY`` names another."""
        raw = os.environ.get(var)
        if not raw:
            raise VaultError(f"{var} not configured")
        keyring = {kid: base64.b64decode(kb64) for kid, kb64 in json.loads(raw).items()}
        active = os.environ.get("SBX_VAULT_ACTIVE_KEY") or next(iter(keyring))
        return cls(keyring, active)

    @property
    def active_key_id(self) -> str:
        return self._active

    def seal(self, plaintext: bytes, *, aad: dict) -> tuple[bytes, str, bytes]:
        """Encrypt -> (ciphertext, key_id, nonce)."""
        aad_b = json.dumps(aad, sort_keys=True, separators=(",", ":")).encode()
        nonce = os.urandom(_NONCE_BYTES)
        ct = AESGCM(self._keyring[self._active]).encrypt(nonce, plaintext, aad_b)
        return ct, self._active, nonce

    def open(self, ciphertext: bytes, *, key_id: str, nonce: bytes, aad: dict) -> bytes:
        key = self._keyring.get(key_id)
        if key is None:
            raise VaultError(f"unknown key id {key_id!r}")
        aad_b = json.dumps(aad, sort_keys=True, separators=(",", ":")).encode()
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, aad_b)
        except InvalidTag as exc:
            raise VaultError("credential ciphertext failed authentication") from exc


def credential_aad(
    *, workspace_id: str, connection_id: str, credential_version_id: str, fmt: str
) -> dict:
    """Required AAD binding per RFC 167 §06."""
    return {
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "credential_version_id": credential_version_id,
        "format": fmt,
    }
