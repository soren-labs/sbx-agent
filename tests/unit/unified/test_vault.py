import os

import pytest
from control.domain.errors import DomainError
from control.security.vault import EnvelopeVault


def test_owner_aad_tamper_and_keyring_rotation():
    old, new = os.urandom(32), os.urandom(32)
    vault = EnvelopeVault({"old": old}, "old")
    context = {"workspace_id": "wsp_one", "connection_id": "con_one", "credential_id": "cred_one"}
    sealed = vault.encrypt({"token": "REDACTED"}, context)
    rotated = EnvelopeVault({"old": old, "new": new}, "new")
    assert rotated.decrypt(sealed, context) == {"token": "REDACTED"}
    assert rotated.encrypt({"token": "REDACTED"}, context)["key_id"] == "new"
    with pytest.raises(DomainError):
        rotated.decrypt(sealed, {**context, "workspace_id": "wsp_other"})
    sealed["ciphertext"] = "tampered"
    with pytest.raises(DomainError):
        rotated.decrypt(sealed, context)
