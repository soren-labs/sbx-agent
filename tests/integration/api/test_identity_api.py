"""Product auth: email/password, verification, cookie+CSRF, API keys, rate limits (RFC 06)."""

from __future__ import annotations

import pytest
from tests.support.api import ApiStack, User


@pytest.fixture
def stack(db, tmp_path):
    s = ApiStack(db, tmp_path)
    yield s
    s.shutdown()


def test_register_verify_login_and_principal_parity(stack) -> None:
    http = stack.client()
    email, password = "ana@example.test", "long enough pw 123"
    assert (
        http.post("/api/auth/register", json={"email": email, "password": password}).status_code
        == 202
    )
    again = http.post("/api/auth/register", json={"email": email, "password": "different pw 999"})
    assert again.status_code == 202, "no account enumeration"
    refused = http.post("/api/auth/login", json={"email": email, "password": password})
    assert refused.status_code == 403 and refused.json()["error"]["action"] == "verify_email"
    token = stack.services.mailer.latest(email)["body"].split("token=")[1].strip()
    assert (
        http.post("/api/auth/email-verifications", json={"token": token}).json()["status"]
        == "verified"
    )
    assert http.post("/api/auth/email-verifications", json={"token": token}).status_code == 422, (
        "single use"
    )
    login = http.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    assert "sbx_session" in login.cookies and login.json()["user"]["email"] == email
    stored = stack.db.read(lambda u: u.find_one("login_sessions", {}))
    assert login.cookies["sbx_session"] not in str(stored), "only the token hash is stored"
    user_id = http.get("/api/me").json()["user"]["id"]
    csrf = login.json()["csrf_token"]
    no_csrf = http.post("/api/api-keys", json={"name": "ci"}, headers={"Idempotency-Key": "k"})
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "csrf_failed"
    bad_origin = http.post(
        "/api/api-keys",
        json={"name": "ci"},
        headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"},
    )
    assert bad_origin.json()["error"]["code"] == "csrf_failed"
    created = http.post("/api/api-keys", json={"name": "ci"}, headers={"X-CSRF-Token": csrf})
    assert created.status_code == 201
    key = created.json()["key"]
    listed = http.get("/api/api-keys").json()["items"]
    assert key not in str(listed) and listed[0]["prefix"] == key[:14]
    bearer = stack.client()
    me = bearer.get("/api/me", headers={"Authorization": f"Bearer {key}"}).json()
    assert me["user"]["id"] == user_id and me["auth"]["via"] == "api_key"
    # API keys need no CSRF; cookie auth does.
    assert (
        bearer.post(
            "/api/api-keys", json={"name": "x"}, headers={"Authorization": f"Bearer {key}"}
        ).status_code
        == 201
    )
    http.delete(f"/api/api-keys/{created.json()['id']}", headers={"X-CSRF-Token": csrf})
    assert bearer.get("/api/me", headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert http.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert http.get("/api/me").status_code == 401


def test_login_rate_limit_and_password_change_revokes_sessions(stack) -> None:
    user = User(stack)
    other = stack.client()
    for _ in range(10):
        assert (
            other.post(
                "/api/auth/login", json={"email": user.email, "password": "wrong password!"}
            ).status_code
            == 401
        )
    limited = other.post("/api/auth/login", json={"email": user.email, "password": user.password})
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "rate_limited"
    changed = user.post(
        "/api/auth/password-changes",
        {"current_password": user.password, "new_password": "brand new password 7"},
    )
    assert changed.status_code == 200
    assert user.get("/api/me").status_code == 401, "auth epoch bump revokes cookie sessions"


def test_password_reset_flow(stack) -> None:
    user = User(stack)
    http = stack.client()
    http.post("/api/auth/password-resets", json={"email": user.email})
    token = stack.services.mailer.latest(user.email)["body"].split("token=")[1].strip()
    assert (
        http.post(
            "/api/auth/password-resets",
            json={"token": token, "new_password": "reset password 1234"},
        ).status_code
        == 200
    )
    assert (
        http.post(
            "/api/auth/login", json={"email": user.email, "password": "reset password 1234"}
        ).status_code
        == 200
    )


def test_validation_errors_never_echo_input(stack) -> None:
    user = User(stack)
    secret = "sk-very-secret-value-0000000000"
    r = user.post(
        f"/api/workspaces/{user.workspace_id}/connections",
        {"kind": "opencode_zen", "credential": {"api_key": 12345, "extra": secret}},
    )
    assert r.status_code == 422 and secret not in r.text
    r = user.post(
        f"/api/workspaces/{user.workspace_id}/connections",
        {"kind": "github", "credential": {"token": "short"}},
    )
    assert r.status_code == 422 and "short" not in r.text
