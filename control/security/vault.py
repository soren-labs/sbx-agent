"""Authenticated encryption; keyring is supplied outside PostgreSQL/backups."""

import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from control.domain.errors import DomainError
from control.domain.events import canonical


class EnvelopeVault:
    def __init__(self, keys: dict[str, bytes], active="1"):
        if active not in keys or any(len(key) != 32 for key in keys.values()):
            raise ValueError("Vault requires 256-bit keys")
        self.keys, self.active = keys, active

    def encrypt(self, payload, context):
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.keys[self.active]).encrypt(
            nonce, canonical(payload).encode(), canonical(context).encode()
        )
        return {
            "key_id": self.active,
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
            "format": "aes256gcm-v1",
        }

    def decrypt(self, envelope, context):
        try:
            body = AESGCM(self.keys[envelope["key_id"]]).decrypt(
                base64.b64decode(envelope["nonce"]),
                base64.b64decode(envelope["ciphertext"]),
                canonical(context).encode(),
            )
            return json.loads(body)
        except Exception:
            raise DomainError("credential_invalid") from None
