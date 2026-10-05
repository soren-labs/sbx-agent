import os

import pytest
from control.application.connections import Connections
from control.application.identity import Identity
from control.application.projects import Projects
from control.application.sessions import Sessions
from control.domain.errors import DomainError
from control.domain.identity import Principal
from control.jobs.claims import Claims
from control.jobs.handlers.connections import ValidationHandler
from control.security.vault import EnvelopeVault


@pytest.fixture
def connections(database):
    return Connections(database, EnvelopeVault({"1": os.urandom(32)}))


def test_password_verification_cookie_and_owner_isolation(database):
    identity = Identity(database)
    user = identity.register("one@example.test", "REDACTED")
    with pytest.raises(DomainError, match="email_verification_required"):
        identity.login("one@example.test", "REDACTED")
    proof = identity.issue_verification(user["user_id"])
    identity.verify_email(proof)
    with pytest.raises(DomainError):
        identity.verify_email(proof)
    cookie, csrf = identity.login("one@example.test", "REDACTED")
    principal = identity.authenticate(cookie)
    assert principal.workspace_ids == (user["workspace_id"],)
    identity.verify_csrf(cookie, csrf)
    with pytest.raises(DomainError):
        identity.verify_csrf(cookie, "wrong")
    identity.logout(cookie)
    with pytest.raises(DomainError):
        identity.authenticate(cookie)
    with database.transaction() as repo:
        assert repo.one("SELECT hash FROM password_credentials")["hash"].startswith("$argon2id$")
        assert repo.one("SELECT token_hash FROM login_sessions")["token_hash"] != cookie


def test_encryption_replacement_stale_validation_and_revocation(database, principal, connections):
    wid = principal.workspace_ids[0]
    body = {"kind": "opencode_zen", "credential": {"api_key": "REDACTED"}}
    created = connections.create(principal, wid, body, "connect")
    cid = created["connection_id"]
    assert connections.create(principal, wid, body, "connect") == created
    with database.transaction() as repo:
        stored = repo.one("SELECT * FROM credential_versions")
        assert "REDACTED" not in str(stored["envelope"])
    assert connections.resolve(principal, cid, "inference")[0] == {"api_key": "REDACTED"}
    with pytest.raises(DomainError, match="forbidden"):
        connections.resolve(principal, cid, "compute")
    with pytest.raises(DomainError, match="not_found"):
        connections.resolve(Principal("usr_other", ()), cid, "inference")
    claims = Claims(database)
    old_claim = claims.take("validation")
    replacement = connections.replace(
        principal, cid, {"credential": {"api_key": "REDACTED"}, "expected_version": 1}, "replace"
    )

    class NeverProbe:
        def validate(self, credential):
            raise AssertionError("stale version must not probe")

    ValidationHandler(database, claims, connections, {"opencode_zen": NeverProbe()})(old_claim)
    assert connections.safe(principal, cid)["health"] == "unverified"
    claims.finish(old_claim)
    connections.revoke(principal, cid, replacement["version"], "disconnect")
    with pytest.raises(DomainError, match="connection_revoked"):
        connections.resolve(principal, cid, "inference")
    with database.transaction() as repo:
        assert all(r["revoked_at"] is not None for r in repo.all("SELECT * FROM credential_grants"))


def test_modal_disconnect_does_not_orphan_compute(database, principal, connections):
    cid = connections.create(
        principal,
        principal.workspace_ids[0],
        {"kind": "modal", "credential": {"token_id": "REDACTED", "token_secret": "REDACTED"}},
        "modal",
    )["connection_id"]
    sessions = Sessions(database)
    sid = sessions.create(
        principal, principal.workspace_ids[0], {"modal_connection_id": cid}, "session"
    )["session_id"]
    with database.transaction() as repo:
        repo.execute(
            "INSERT INTO executor_leases(id,workspace_id,session_id,backend,generation"
            ",allocation_operation_id,connection_id,expires_at) "
            "VALUES('lease_test',%s,%s,'modal',1,'allocation_test',%s,now()+interval '1 hour')",
            (principal.workspace_ids[0], sid, cid),
        )
    with pytest.raises(DomainError, match="dependent_resources_active"):
        connections.revoke(principal, cid, 1, "revoke")
    assert connections.resolve(principal, cid, "teardown")[0]["token_id"] == "REDACTED"
    with database.transaction() as repo:
        repo.execute("UPDATE executor_leases SET state='released',cleanup_confirmed=true")
    assert connections.revoke(principal, cid, 1, "revoke")["state"] == "revoked"


def test_project_versions_are_immutable_and_pinned(database, principal):
    projects = Projects(database)
    initial = projects.create(
        principal,
        principal.workspace_ids[0],
        {
            "repository": "https://github.com/soren-labs/sbx-e2e-test",
            "spec": {"env": {"LANG": "C.UTF-8"}},
        },
        "project",
    )
    sid = Sessions(database).create(
        principal,
        principal.workspace_ids[0],
        {"project_version_id": initial["project_version_id"]},
        "session",
    )["session_id"]
    replacement = projects.publish(
        principal,
        initial["project_id"],
        {
            "expected_version": 1,
            "repository": "https://github.com/soren-labs/sbx-e2e-test",
            "spec": {"env": {"LANG": "en_US.UTF-8"}},
        },
        "publish",
    )
    assert replacement["project_version_id"] != initial["project_version_id"]
    assert (
        Sessions(database).get(principal, sid)["project_version_id"]
        == initial["project_version_id"]
    )
    with pytest.raises(DomainError):
        projects.create(
            principal,
            principal.workspace_ids[0],
            {"repository": "https://REDACTED@github.com/user/repo"},
            "bad",
        )


def test_concurrent_last_provider_slot_does_not_oversell(database, principal, connections):
    from concurrent.futures import ThreadPoolExecutor

    from control.application.execution import Execution

    wid = principal.workspace_ids[0]
    cid = connections.create(
        principal, wid, {"kind": "opencode_zen", "credential": {"api_key": "REDACTED"}}, "zen"
    )["connection_id"]
    with database.transaction() as repo:
        repo.execute("UPDATE connections SET concurrency_limit=1 WHERE id=%s", (cid,))
        repo.execute("UPDATE jobs SET state='succeeded'")
    sessions = Sessions(database)
    for i in range(2):
        sid = sessions.create(
            principal, wid, {"backend": "local", "zen_connection_id": cid}, f"c{i}"
        )["session_id"]
        sessions.send(principal, sid, {"content": "work"}, f"m{i}")
    claims = Claims(database)
    claimed = [claims.take("first"), claims.take("second")]

    def admit(claim):
        try:
            return Execution(database, claims).admit(claim)[2]["id"]
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, claimed))
    assert results.count("waiting_capacity") == 1
    with database.transaction() as repo:
        assert (
            repo.one("SELECT count(*) AS n FROM capacity_reservations WHERE state='active'")["n"]
            == 1
        )
        assert repo.one("SELECT count(*) AS n FROM turns WHERE state='queued'")["n"] == 1
        assert repo.one("SELECT count(*) AS n FROM executor_leases")["n"] == 1
