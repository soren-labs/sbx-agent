"""Authenticated envelope encryption for CredentialVersion (RFC 06).

AES-256-GCM with a keyring held outside the database. Associated data binds
workspace, Connection, CredentialVersion and format, so ciphertext moved to
another row cannot be decrypted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from control.domain.digests import canonical_json


class VaultError(Exception):
    pass


@dataclass(frozen=True)
class Sealed:
    key_id: str
    nonce: bytes
    ciphertext: bytes


class Vault:
    def __init__(self, keyring: dict[str, bytes], active_key_id: str) -> None:
        if active_key_id not in keyring:
            raise VaultError("active key missing from keyring")
        for kid, key in keyring.items():
            if len(key) != 32:
                raise VaultError(f"vault key {kid} must be 32 bytes")
        self.keyring = dict(keyring)
        self.active = active_key_id

    @classmethod
    def from_spec(cls, spec: str) -> Vault:
        """``kid:base64key[,kid:base64key...]``; the first entry is active."""
        ring: dict[str, bytes] = {}
        order: list[str] = []
        for item in spec.split(","):
            kid, _, b64 = item.strip().partition(":")
            if not kid or not b64:
                raise VaultError("malformed vault key spec")
            ring[kid] = base64.b64decode(b64)
            order.append(kid)
        return cls(ring, order[0])

    @staticmethod
    def generate_spec(kid: str = "k1") -> str:
        return f"{kid}:{base64.b64encode(os.urandom(32)).decode()}"

    @staticmethod
    def aad(workspace_id: str, connection_id: str, credential_version_id: str, fmt: str) -> bytes:
        return canonical_json(
            {
                "workspace_id": workspace_id,
                "connection_id": connection_id,
                "credential_version_id": credential_version_id,
                "format": fmt,
            }
        )

    def seal(self, plaintext: dict[str, Any], aad: bytes) -> Sealed:
        nonce = os.urandom(12)
        data = json.dumps(plaintext, separators=(",", ":")).encode()
        return Sealed(
            self.active, nonce, AESGCM(self.keyring[self.active]).encrypt(nonce, data, aad)
        )

    def open(self, sealed: Sealed, aad: bytes) -> dict[str, Any]:
        key = self.keyring.get(sealed.key_id)
        if key is None:
            raise VaultError("ciphertext key is not in the keyring")
        try:
            return json.loads(
                AESGCM(key).decrypt(bytes(sealed.nonce), bytes(sealed.ciphertext), aad)
            )
        except InvalidTag as exc:
            raise VaultError("ciphertext authentication failed") from exc

    def fingerprint(self, material: str) -> str:
        """Keyed, non-reversible identifier for change detection; never exposed by the API."""
        return hmac.new(self.keyring[self.active], material.encode(), hashlib.sha256).hexdigest()[
            :24
        ]
