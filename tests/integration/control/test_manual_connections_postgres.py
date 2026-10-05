"""Optional local PostgreSQL acceptance, with no cloud or production credentials."""

import os
import secrets

import pytest
from control.app import create_app
from control.auth_store import AuthDatabase, AuthStore, PersistentApiKeyStore
from control.connections import SecretVault
from fastapi.testclient import TestClient


def test_manual_connections_survive_control_restart_and_are_user_scoped():
    url = os.environ.get("SBX_TEST_MANUAL_POSTGRES_URL")
    if not url:
        pytest.skip("set SBX_TEST_MANUAL_POSTGRES_URL to a disposable local PostgreSQL")
    auth = AuthStore(AuthDatabase(database_url=url))
    users = [auth.create_user() for _ in range(2)]
    credentials = [secrets.token_urlsafe(32) for _ in users]
    vault = SecretVault(secrets.token_bytes(32))

    class Provider:
        def validate(self, secret):
            assert secret in credentials
            return {"models": ["opencode/free"], "repositories": []}

    def factory():
        return create_app(
            auth_store=AuthStore(AuthDatabase(database_url=url)),
            hosted=True,
            state_backend="postgres",
            connection_vault=vault,
            github_token_provider=Provider(),
            zen_provider=Provider(),
        )

    headers = [
        {"Authorization": f"Bearer {PersistentApiKeyStore(auth).create(user_id=u.id)[1]}"}
        for u in users
    ]
    first = factory()
    with TestClient(first) as client:
        for user, token, header in zip(users, credentials, headers, strict=True):
            for provider, field in (("github", "token"), ("opencode", "api_key")):
                result = client.post(
                    f"/hosted/connections/{provider}", json={field: token}, headers=header
                )
                assert result.status_code == 200, result.text
                assert token not in result.text
        with auth.database.transaction() as conn:
            rows = auth.database.execute(
                conn,
                "SELECT credential_cipher FROM hosted_connections WHERE user_id = ?",
                (users[0].id,),
            ).fetchall()
        assert rows and all(credentials[0] not in row["credential_cipher"] for row in rows)
    restored = factory()
    with TestClient(restored) as client:
        for user, token, header in zip(users, credentials, headers, strict=True):
            for provider in ("github", "opencode"):
                result = client.get(f"/hosted/connections/{provider}", headers=header)
                assert result.json()["connection"]["state"] == "connected"
                assert token not in result.text
            assert restored.state.manual_connections.secret(user.id, "opencode") == token
        for provider in ("github", "opencode"):
            assert (
                client.request(
                    "DELETE", f"/hosted/connections/{provider}", json={}, headers=headers[0]
                ).status_code
                == 200
            )
            assert (
                client.get(f"/hosted/connections/{provider}", headers=headers[1]).json()[
                    "connection"
                ]["state"]
                == "connected"
            )
