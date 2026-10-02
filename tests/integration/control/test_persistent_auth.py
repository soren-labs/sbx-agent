"""Real /v1 authentication across app reconstruction with isolated SQLite storage."""

from __future__ import annotations

import secrets
import sqlite3

from control.api_v1.deps import get_key_store
from control.app import create_app
from control.auth_store import AuthDatabase, AuthStore, PersistentApiKeyStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request


def test_api_created_key_and_revocation_survive_control_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("SBX_AUTH_DB_PATH", str(tmp_path / "auth.sqlite3"))
    bootstrap = f"sbx_{secrets.token_hex(32)}"
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", bootstrap)
    headers = {"Authorization": f"Bearer {bootstrap}"}

    with TestClient(create_app()) as client:
        response = client.post("/v1/api-keys", headers=headers, json={"label": "persistent"})
        assert response.status_code == 201
        key = response.json()
        assert set(key) == {"id", "label", "scopes", "created_at", "revoked_at", "key"}
        token = key["key"]
    with TestClient(create_app()) as client:
        assert client.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        listed = client.get("/v1/api-keys", headers=headers).json()["api_keys"]
        assert key["id"] in {record["id"] for record in listed}
        assert token not in str(listed)
        assert all("key_hash" not in record and "key" not in record for record in listed)
        assert client.delete(f"/v1/api-keys/{key['id']}", headers=headers).status_code == 204
    with TestClient(create_app()) as client:
        response = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401


def test_console_grant_keeps_one_time_operator_semantics_and_durable_key(tmp_path, monkeypatch):
    path = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("SBX_AUTH_DB_PATH", str(path))
    token = f"sbx_{secrets.token_hex(32)}"
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", token)
    with TestClient(create_app()) as client:
        grant = client.post("/v1/console/grant", headers={"Authorization": f"Bearer {token}"})
        assert grant.status_code == 201
        payload = {"grant": grant.json()["grant"]}
        exchange = client.post("/v1/console/exchange", json=payload)
        assert exchange.status_code == 201
        assert set(exchange.json()["scopes"]) == {"agents", "admin"}
        console_token = exchange.json()["key"]
        assert client.post("/v1/console/exchange", json=payload).status_code == 401
    with TestClient(create_app()) as client:
        assert (
            client.get(
                "/v1/api-keys", headers={"Authorization": f"Bearer {console_token}"}
            ).status_code
            == 200
        )
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert conn.execute("SELECT user_id FROM api_keys").fetchall() == [(None,)]


def test_product_key_survives_bootstrap_rotation_and_removal(tmp_path, monkeypatch):
    monkeypatch.setenv("SBX_AUTH_DB_PATH", str(tmp_path / "auth.sqlite3"))
    old = f"sbx_{secrets.token_hex(32)}"
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", old)
    app = create_app()
    _, product = app.state.api_key_store.create()
    new = f"sbx_{secrets.token_hex(32)}"
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", new)
    with TestClient(create_app()) as client:
        assert client.get("/v1/me", headers={"Authorization": f"Bearer {old}"}).status_code == 401
        assert client.get("/v1/me", headers={"Authorization": f"Bearer {new}"}).status_code == 200
        assert (
            client.get("/v1/me", headers={"Authorization": f"Bearer {product}"}).status_code == 200
        )
    monkeypatch.delenv("SBX_V1_BOOTSTRAP_KEY")
    with TestClient(create_app()) as client:
        assert client.get("/v1/me", headers={"Authorization": f"Bearer {new}"}).status_code == 401
        assert (
            client.get("/v1/me", headers={"Authorization": f"Bearer {product}"}).status_code == 200
        )


def test_auth_dependency_fallback_is_durable_and_explicit_injection_is_preserved(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SBX_AUTH_DB_PATH", str(tmp_path / "auth.sqlite3"))
    app = FastAPI()
    request = Request({"type": "http", "app": app})
    keys = get_key_store(request)
    assert isinstance(keys, PersistentApiKeyStore)
    _, token = keys.create()
    next_app = FastAPI()
    assert get_key_store(Request({"type": "http", "app": next_app})).lookup(token) is not None
    auth = AuthStore(AuthDatabase(path=tmp_path / "injected.sqlite3"))
    injected = create_app(auth_store=auth)
    assert injected.state.auth_store is auth
    assert injected.state.api_key_store.auth is auth
    assert not (tmp_path / "injected.sqlite3").exists()  # lazy construction


def test_durable_auth_storage_failure_does_not_fall_back_to_memory(tmp_path):
    path = tmp_path / "auth.sqlite3"
    auth = AuthStore(AuthDatabase(path=path))
    _, token = PersistentApiKeyStore(auth).create()
    # Reconstruct against a corrupt file: product auth must fail closed.
    path.write_bytes(b"invalid database")
    app = create_app(auth_store=AuthStore(AuthDatabase(path=path)))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 500
    assert isinstance(app.state.api_key_store, PersistentApiKeyStore)
