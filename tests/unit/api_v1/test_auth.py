"""Bearer auth: 401 / 403 canonical bodies, scope enforcement, /v1/me."""

from __future__ import annotations


class TestBearerAuth:
    def test_missing_header_is_401(self, client) -> None:
        resp = client.get("/v1/me")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"
        assert "message" in resp.json()["error"]

    def test_unknown_token_is_401(self, client) -> None:
        resp = client.get("/v1/me", headers={"Authorization": "Bearer sbx_nope"})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    def test_non_bearer_scheme_is_401(self, client) -> None:
        resp = client.get("/v1/me", headers={"Authorization": "Basic c2J4OnNieA=="})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    def test_all_v1_routes_require_auth(self, client) -> None:
        for method, path in (
            ("get", "/v1/agents"),
            ("post", "/v1/agents"),
            ("get", "/v1/models"),
            ("get", "/v1/me"),
            ("get", "/v1/accounts"),
            ("post", "/v1/accounts"),
            ("get", "/v1/api-keys"),
            ("post", "/v1/api-keys"),
            ("get", "/v1/agents/x"),
            ("delete", "/v1/agents/x"),
            ("get", "/v1/agents/x/runs"),
            ("post", "/v1/agents/x/runs"),
            ("get", "/v1/agents/x/runs/run-1"),
            ("post", "/v1/agents/x/runs/run-1/cancel"),
            ("get", "/v1/agents/x/runs/run-1/stream"),
            ("get", "/v1/agents/x/usage"),
        ):
            resp = getattr(client, method)(path)
            assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"
            assert resp.json()["error"]["code"] == "unauthorized"

    def test_revoked_key_is_401(self, client, v1_env) -> None:
        record, token = v1_env.keys.create(label="tmp", scopes=("agents",))
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/v1/me", headers=headers).status_code == 200
        assert v1_env.keys.revoke(record.id)
        assert client.get("/v1/me", headers=headers).status_code == 401


class TestScopes:
    def test_me_returns_key_identity(self, client, auth, v1_env) -> None:
        resp = client.get("/v1/me", headers=auth)
        assert resp.status_code == 200
        body = resp.json()
        assert body["key_id"] == v1_env.agents_key_id
        assert body["label"] == "agents-key"
        assert body["scopes"] == ["agents"]

    def test_agents_scope_cannot_reach_admin(self, client, auth) -> None:
        for method, path in (
            ("get", "/v1/accounts"),
            ("post", "/v1/accounts"),
            ("get", "/v1/api-keys"),
            ("post", "/v1/api-keys"),
        ):
            resp = getattr(client, method)(path, headers=auth)
            assert resp.status_code == 403, f"{method} {path}"
            assert resp.json()["error"]["code"] == "forbidden"

    def test_admin_scope_reaches_admin(self, client, admin_auth) -> None:
        assert client.get("/v1/accounts", headers=admin_auth).status_code == 200
        assert client.get("/v1/api-keys", headers=admin_auth).status_code == 200

    def test_admin_only_key_lacks_agents_scope(self, client, v1_env) -> None:
        _, token = v1_env.keys.create(label="ops", scopes=("admin",))
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/v1/accounts", headers=headers).status_code == 200
        resp = client.get("/v1/agents", headers=headers)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"
        # /v1/me only needs a valid key, not a scope
        assert client.get("/v1/me", headers=headers).status_code == 200
