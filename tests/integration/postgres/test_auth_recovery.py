import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from control.application.identity import Identity
from control.domain.errors import DomainError
from control.jobs.claims import Claims
from control.jobs.handlers.identity import NoticeHandler
from control.security.vault import EnvelopeVault
from control.tooling.grants import ToolGrants
from tests.integration.postgres.test_execution import prepare


def test_auth_receipts_encrypted_notice_restart_and_password_reset(database):
    master = os.urandom(32)
    vault = EnvelopeVault({"1": master})
    identity = Identity(database, vault, master)
    identity.email_enabled = True
    user = identity.register("mail@example.test", "REDACTED", key="register")
    rebuilt = Identity(database, vault, master)
    rebuilt.email_enabled = True
    assert rebuilt.register("mail@example.test", "REDACTED", key="register") == user
    sent = []
    claims = Claims(database)
    notice = NoticeHandler(database, claims, vault, lambda *args: sent.append(args))
    claim = claims.take("email")
    notice(claim)
    claims.finish(claim)
    assert sent[0][1] == "verify"
    rebuilt.verify_email(sent[0][2])
    with ThreadPoolExecutor(max_workers=2) as pool:
        cookies = list(
            pool.map(
                lambda _: rebuilt.login("mail@example.test", "REDACTED", key="login"), range(2)
            )
        )
    assert cookies[0] == cookies[1]
    assert rebuilt.authenticate(cookies[0][0]).user_id == user["user_id"]
    with pytest.raises(DomainError, match="idempotency_conflict"):
        rebuilt.login("mail@example.test", "another-password", key="login")
    assert rebuilt.request_reset("unknown@example.test", "unknown") == {"accepted": True}
    rebuilt.request_reset("mail@example.test", "reset")
    claim = claims.take("reset-email")
    notice(claim)
    claims.finish(claim)
    assert sent[1][1] == "reset"
    assert rebuilt.reset_password(sent[1][2], "REDACTED")["changed"]
    with pytest.raises(DomainError, match="credential_invalid"):
        rebuilt.reset_password(sent[1][2], "REDACTED")
    with pytest.raises(DomainError, match="forbidden"):
        rebuilt.authenticate(cookies[0][0])
    with database.transaction() as repo:
        # Only nonce/ciphertext is persisted, not verifier or login-cookie material.
        encoded = str(repo.all("SELECT envelope FROM identity_notices"))
        assert sent[0][2] not in encoded and sent[1][2] not in encoded
        assert repo.one("SELECT count(*) AS n FROM users")["n"] == 1


def test_tool_grants_are_execution_lease_action_and_expiry_scoped(database, principal):
    sessions, claim, execution, lease, ingest = prepare(database, principal)
    grants = ToolGrants(
        SimpleNamespace(uow=database, sessions=sessions),
        os.urandom(32),
        "https://control.example.test",
    )
    session = sessions.get(principal, lease["session_id"])
    issue = grants.issue(session, execution, lease)
    assert grants.invoke(issue["token"], "read", {}, "read")["id"] == session["id"]
    with pytest.raises(DomainError, match="forbidden"):
        grants.invoke(issue["token"], "connections", {}, "secrets")
    with pytest.raises(DomainError, match="forbidden"):
        grants.invoke(issue["token"], "read", {"session_id": "unrelated"}, "read-other")
    with database.transaction() as repo:
        repo.execute("UPDATE executor_leases SET state='lost'")
    with pytest.raises(DomainError, match="forbidden"):
        grants.invoke(issue["token"], "read", {}, "expired-lease")
