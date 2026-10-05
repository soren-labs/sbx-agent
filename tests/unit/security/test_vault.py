"""Vault envelope semantics (RFC 167 §06): AAD-bound AES-GCM, keyring
outside the DB, bounded rotation."""

import pytest
from control.domain.errors import DomainError
from control.security.vault import Vault, credential_aad

pytestmark = pytest.mark.unit


class TestSealOpen:
    def test_roundtrip(self):
        v = Vault.generate()
        aad = credential_aad(
            workspace_id="wsp_1",
            connection_id="con_1",
            credential_version_id="cred_1",
            fmt="api_key",
        )
        ct, key_id, nonce = v.seal(b'{"api_key":"x"}', aad=aad)
        assert ct != b'{"api_key":"x"}'
        assert v.open(ct, key_id=key_id, nonce=nonce, aad=aad) == b'{"api_key":"x"}'

    def test_aad_binding_rejects_replay_under_other_owner(self):
        v = Vault.generate()
        aad = credential_aad(
            workspace_id="wsp_1",
            connection_id="con_1",
            credential_version_id="cred_1",
            fmt="api_key",
        )
        ct, key_id, nonce = v.seal(b"secret", aad=aad)
        other = credential_aad(
            workspace_id="wsp_2",  # different owner
            connection_id="con_1",
            credential_version_id="cred_1",
            fmt="api_key",
        )
        with pytest.raises(DomainError):
            v.open(ct, key_id=key_id, nonce=nonce, aad=other)

    def test_wrong_key_id_fails(self):
        v = Vault.generate()
        aad = {"a": 1}
        ct, key_id, nonce = v.seal(b"x", aad=aad)
        with pytest.raises(DomainError):
            v.open(ct, key_id="k_unknown", nonce=nonce, aad=aad)

    def test_rotation_old_key_still_opens(self):
        v1 = Vault.generate("k1")
        aad = {"a": 1}
        ct, key_id, nonce = v1.seal(b"payload", aad=aad)
        # Rotate: k1 still decrypts, k2 seals.
        v2 = Vault({**v1._keyring, "k2": __import__("os").urandom(32)}, "k2")
        assert v2.open(ct, key_id="k1", nonce=nonce, aad=aad) == b"payload"
        ct2, key_id2, _ = v2.seal(b"new", aad=aad)
        assert key_id2 == "k2"
        # Old vault cannot read k2 ciphertext.
        with pytest.raises(DomainError):
            v1.open(ct2, key_id="k2", nonce=_, aad=aad)
