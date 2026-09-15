"""Admin endpoints: accounts CRUD + verify, api-keys lifecycle."""

from __future__ import annotations


class TestAccounts:
    def test_create_list_get(self, client, admin_auth) -> None:
        resp = client.post(
            "/v1/accounts",
            json={
                "provider": "grok",
                "label": "grok acct",
                "credential": {"files": {".grok/creds.json": "SECRET-CONTENT"}},
                "max_concurrent": 3,
                "models": ["grok-4.1"],
            },
            headers=admin_auth,
        )
        assert resp.status_code == 201, resp.text
        account = resp.json()
        assert account["provider"] == "grok"
        assert account["label"] == "grok acct"
        assert account["status"] == "active"
        assert account["max_concurrent"] == 3
        assert account["models"] == ["grok-4.1"]
        assert account["running"] == 0
        # credential material is never echoed back
        assert "SECRET-CONTENT" not in resp.text
        assert "credential" not in account

        listing = client.get("/v1/accounts?provider=grok", headers=admin_auth).json()
        assert [a["id"] for a in listing["accounts"]] == [account["id"]]
        listing = client.get("/v1/accounts?provider=grok", headers=admin_auth).json()
        assert "SECRET-CONTENT" not in str(listing)

        got = client.get(f"/v1/accounts/{account['id']}", headers=admin_auth)
        assert got.status_code == 200
        assert got.json()["id"] == account["id"]

    def test_credential_blob_is_stored_not_echoed(self, client, admin_auth, v1_env) -> None:
        resp = client.post(
            "/v1/accounts",
            json={
                "provider": "grok",
                "label": "g",
                "credential": {"files": {".grok/creds.json": "SECRET-CONTENT"}},
            },
            headers=admin_auth,
        )
        account_id = resp.json()["id"]
        blob = v1_env.registry.get_credential_blob(account_id)
        assert blob == {
            "provider": "grok",
            "files": {".grok/creds.json": "SECRET-CONTENT"},
        }

    def test_bad_provider_is_400(self, client, admin_auth) -> None:
        resp = client.post(
            "/v1/accounts",
            json={"provider": "bogus", "label": "x"},
            headers=admin_auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_provider"

    def test_malformed_credential_is_400(self, client, admin_auth) -> None:
        resp = client.post(
            "/v1/accounts",
            json={
                "provider": "grok",
                "label": "x",
                "credential": {"files": {".grok/creds.json": 42}},
            },
            headers=admin_auth,
        )
        assert resp.status_code == 400

    def test_malformed_credential_leaves_no_orphan_account(self, client, admin_auth) -> None:
        """A refused credential must not persist an active, credential-less
        account the scheduler could pick."""
        before = [
            a["id"] for a in client.get("/v1/accounts", headers=admin_auth).json()["accounts"]
        ]
        resp = client.post(
            "/v1/accounts",
            json={
                "provider": "grok",
                "label": "x",
                "credential": {"files": "not-a-string-map"},
            },
            headers=admin_auth,
        )
        assert resp.status_code == 400
        after = [a["id"] for a in client.get("/v1/accounts", headers=admin_auth).json()["accounts"]]
        assert after == before

    def test_get_missing_is_404(self, client, admin_auth) -> None:
        resp = client.get("/v1/accounts/nope", headers=admin_auth)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_delete(self, client, admin_auth) -> None:
        resp = client.post(
            "/v1/accounts",
            json={"provider": "grok", "label": "x"},
            headers=admin_auth,
        )
        account_id = resp.json()["id"]
        assert client.delete(f"/v1/accounts/{account_id}", headers=admin_auth).status_code == 204
        assert client.get(f"/v1/accounts/{account_id}", headers=admin_auth).status_code == 404
        assert client.delete(f"/v1/accounts/{account_id}", headers=admin_auth).status_code == 404

    def test_verify_marks_active(self, client, admin_auth, v1_env) -> None:
        resp = client.post(
            "/v1/accounts",
            json={"provider": "codex", "label": "verify me"},
            headers=admin_auth,
        )
        account_id = resp.json()["id"]
        v1_env.registry.mark_status(account_id, "cooling")
        resp = client.post(f"/v1/accounts/{account_id}/verify", headers=admin_auth)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "active"
        assert resp.json()["last_error"] is None

    def test_verify_missing_is_404(self, client, admin_auth) -> None:
        resp = client.post("/v1/accounts/nope/verify", headers=admin_auth)
        assert resp.status_code == 404


class TestApiKeys:
    def test_create_list_revoke(self, client, admin_auth) -> None:
        resp = client.post(
            "/v1/api-keys",
            json={"label": "ci", "scopes": ["agents"]},
            headers=admin_auth,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["label"] == "ci"
        assert body["scopes"] == ["agents"]
        token = body["key"]
        assert token.startswith("sbx_")

        # plaintext appears exactly once; list shows hashes only
        listing = client.get("/v1/api-keys", headers=admin_auth).json()
        assert token not in str(listing)
        ids = {k["id"] for k in listing["api_keys"]}
        assert body["id"] in ids

        # the minted token authenticates
        me = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["key_id"] == body["id"]

        assert client.delete(f"/v1/api-keys/{body['id']}", headers=admin_auth).status_code == 204
        me = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 401

    def test_create_defaults_and_validation(self, client, admin_auth) -> None:
        resp = client.post("/v1/api-keys", json={}, headers=admin_auth)
        assert resp.status_code == 201
        assert resp.json()["scopes"] == ["agents"]
        resp = client.post("/v1/api-keys", headers=admin_auth)  # no body at all
        assert resp.status_code == 201
        resp = client.post(
            "/v1/api-keys",
            json={"scopes": ["bogus"]},
            headers=admin_auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_provider"

    def test_revoke_missing_is_404(self, client, admin_auth) -> None:
        resp = client.delete("/v1/api-keys/nope", headers=admin_auth)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_admin_endpoints_reject_agents_scope(self, client, auth) -> None:
        resp = client.post(
            "/v1/accounts",
            json={"provider": "codex", "label": "x"},
            headers=auth,
        )
        assert resp.status_code == 403
        resp = client.delete("/v1/api-keys/whatever", headers=auth)
        assert resp.status_code == 403
