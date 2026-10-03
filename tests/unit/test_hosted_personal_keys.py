"""Personal key ownership, expiry, browser auth and split-origin CSRF/CORS."""

import secrets

import pytest
from control.app import create_app
from control.auth_store import AuthDatabase, AuthStore, PersistentApiKeyStore
from control.connections import SecretVault
from control.hosted_auth_routes import COOKIE_NAME
from fastapi.testclient import TestClient


@pytest.fixture
def personal_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SBX_CONNECTIONS_MODE", "mock")
    monkeypatch.setenv("SBX_BROWSER_ORIGINS", "https://sbx-agent.com")
    clock = [1000000.0]
    auth = AuthStore(AuthDatabase(path=tmp_path / "personal.db"), clock=lambda: clock[0])
    users = [auth.create_user(email=f"personal-{i}@example.test") for i in range(2)]
    app = create_app(
        auth_store=auth,
        hosted=True,
        state_backend="postgres",
        connection_vault=SecretVault(secrets.token_bytes(32)),
    )
    return app, users, clock


def sign_in(client, auth, user):
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, auth.create_session(user.id)[1])


def test_personal_keys_shown_once_owned_expire_and_revoke(personal_app):
    app, users, clock = personal_app
    auth = app.state.auth_store
    with TestClient(app, base_url="https://api.sbx-agent.com") as client:
        assert client.get("/hosted/api-keys").status_code == 401
        sign_in(client, auth, users[0])
        created = client.post(
            "/hosted/api-keys",
            json={"label": "CI", "scopes": ["agents"], "expires_in_days": 1},
            headers={"Origin": "https://sbx-agent.com"},
        )
        assert created.status_code == 201
        assert created.headers["Cache-Control"] == "no-store"
        record = created.json()
        assert record["key"].startswith("sbx_")
        assert "key_hash" not in record
        listing = client.get("/hosted/api-keys").json()["api_keys"]
        assert listing == [{key: value for key, value in record.items() if key != "key"}]
        assert record["key"] not in str(listing)
        store = PersistentApiKeyStore(auth)
        assert store.lookup(record["key"]).user_id == users[0].id
        clock[0] += 86400
        assert store.lookup(record["key"]) is None
        sign_in(client, auth, users[1])
        assert client.get("/hosted/api-keys").json()["api_keys"] == []
        assert (
            client.request("DELETE", f"/hosted/api-keys/{record['id']}", json={}).status_code == 404
        )
        sign_in(client, auth, users[0])
        for _ in range(2):
            assert (
                client.request("DELETE", f"/hosted/api-keys/{record['id']}", json={}).status_code
                == 204
            )
        assert client.get("/hosted/api-keys").json()["api_keys"][0]["revoked_at"]


def test_personal_keys_cannot_grant_admin_or_manage_keys_by_bearer(personal_app):
    app, users, _ = personal_app
    with TestClient(app, base_url="https://api.sbx-agent.com") as client:
        sign_in(client, app.state.auth_store, users[0])
        assert client.post("/hosted/api-keys", json={"scopes": ["admin"]}).status_code == 422
        assert client.post("/hosted/api-keys", json={"expires_in_days": 0}).status_code == 422
        token = client.post("/hosted/api-keys", json={"expires_in_days": None}).json()["key"]
        client.cookies.clear()
        assert (
            client.post(
                "/hosted/api-keys", json={}, headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 401
        )
        assert (
            client.get("/v1/api-keys", headers={"Authorization": f"Bearer {token}"}).status_code
            == 403
        )
        assert client.get("/api/sessions", auth=("sbx", "sbx")).status_code == 404


def test_static_origin_has_exact_credentialed_cors_and_json_csrf(personal_app):
    app, users, _ = personal_app
    with TestClient(app, base_url="https://api.sbx-agent.com") as client:
        sign_in(client, app.state.auth_store, users[0])
        cors = client.options(
            "/hosted/api-keys",
            headers={
                "Origin": "https://sbx-agent.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert cors.status_code == 200
        assert cors.headers["Access-Control-Allow-Origin"] == "https://sbx-agent.com"
        assert cors.headers["Access-Control-Allow-Credentials"] == "true"
        assert (
            client.post(
                "/hosted/api-keys", json={}, headers={"Origin": "https://sbx-agent.com.evil.test"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/hosted/api-keys",
                data={"label": "form"},
                headers={"Origin": "https://sbx-agent.com"},
            ).status_code
            == 415
        )
        assert client.get("/hosted/health").json() == {"status": "ready"}


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "https://*.sbx-agent.com",
        "https://sbx-agent.com/path",
        "https://user:REDACTED@sbx-agent.com",
    ],
)
def test_invalid_browser_origins_fail_closed(origin):
    from control.hosted_deployment import configure_browser_origins
    from fastapi import FastAPI

    with pytest.raises(ValueError):
        configure_browser_origins(FastAPI(), origin)
