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
        # SOR-216: credential materialized but never cloud-verified → the
        # account is not scheduler-eligible until /verify passes.
        assert account["status"] == "unverified"
        assert account["auth_state"] == "materialized"
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

    def test_create_with_credential_claims_managed_secret_name(
        self, client, admin_auth, v1_env
    ) -> None:
        """SOR-219 acceptance: an account created with a credential must
        carry the managed ``<prefix><id>`` secret_name — that name is the
        lane mounting the blob into agent sandboxes. Without it every turn
        runs credential-less even though /verify passes."""
        from control.config import account_secret_prefix

        resp = client.post(
            "/v1/accounts",
            json={
                "provider": "grok",
                "label": "x",
                "credential": {"files": {".grok/creds.json": "SECRET-CONTENT"}},
            },
            headers=admin_auth,
        )
        assert resp.status_code == 201, resp.text
        account = v1_env.registry.get(resp.json()["id"])
        assert account.secret_name == f"{account_secret_prefix()}{account.id}"

        # Credential-less creates stay unmanaged — nothing to mount.
        resp = client.post(
            "/v1/accounts",
            json={"provider": "grok", "label": "y"},
            headers=admin_auth,
        )
        assert resp.status_code == 201, resp.text
        assert v1_env.registry.get(resp.json()["id"]).secret_name == ""

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
        v1_env.registry.put_credential_blob(
            account_id, {"provider": "codex", "files": {".codex/auth.json": "REDACTED"}}
        )
        v1_env.registry.mark_status(account_id, "cooling")
        resp = client.post(f"/v1/accounts/{account_id}/verify", headers=admin_auth)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "active"
        assert resp.json()["last_error"] is None

    def test_verify_missing_is_404(self, client, admin_auth) -> None:
        resp = client.post("/v1/accounts/nope/verify", headers=admin_auth)
        assert resp.status_code == 404


class TestAccountIdTraversal:
    """SOR-105: ``{account_id}`` path params reach ``FileAccountStore`` paths;
    a file-backed registry refuses non-conformant ids and the routes map the
    refusal to ``not_found`` — never a 500, never a store access."""

    def _file_registry(self, v1_env, tmp_path):
        from control.accounts import FileAccountStore, PersistentAccountRegistry
        from control.scheduler import AccountScheduler

        registry = PersistentAccountRegistry(FileAccountStore(tmp_path / "accounts"))
        v1_env.app.state.account_registry = registry
        v1_env.app.state.scheduler = AccountScheduler(registry)
        return registry

    def test_traversal_ids_are_not_found_not_500(
        self, client, admin_auth, v1_env, tmp_path
    ) -> None:
        self._file_registry(v1_env, tmp_path)
        # Ids that survive URL transport and still fail the account_id rule.
        for bad in (".hidden", "-x", "a%5Cb", "a%20b", "x" * 129):
            resp = client.get(f"/v1/accounts/{bad}", headers=admin_auth)
            assert resp.status_code == 404, (bad, resp.text)
            resp = client.delete(f"/v1/accounts/{bad}", headers=admin_auth)
            assert resp.status_code == 404, (bad, resp.text)
            resp = client.post(f"/v1/accounts/{bad}/verify", headers=admin_auth)
            assert resp.status_code == 404, (bad, resp.text)
        # Nothing ever reached the file store (tmp_path/home is the test
        # isolation HOME, not store state).
        assert not (tmp_path / "accounts").exists()

    def test_create_agent_with_traversal_account_id_is_409(
        self, client, admin_auth, v1_env, tmp_path
    ) -> None:
        self._file_registry(v1_env, tmp_path)
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "hi"},
                "agent": {"provider": "grok", "account_id": "../escape"},
            },
            headers=admin_auth,
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "account_unavailable"
        assert not (tmp_path / "accounts").exists()

    def test_record_with_smuggled_body_id_is_not_500(
        self, client, admin_auth, v1_env, tmp_path
    ) -> None:
        """SOR-105 review: a stored record whose body id is unsafe or
        foreign to its key decodes as a disabled corrupt record — GET
        returns it keyed by its store key, never a 500."""
        from control.accounts import FileAccountStore, PersistentAccountRegistry
        from control.scheduler import AccountScheduler

        store = FileAccountStore(tmp_path / "accounts")
        registry = PersistentAccountRegistry(store)
        v1_env.app.state.account_registry = registry
        v1_env.app.state.scheduler = AccountScheduler(registry)
        store.put_record("good-1", {"id": "../victim", "provider": "grok", "status": "active"})
        resp = client.get("/v1/accounts/good-1", headers=admin_auth)
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == "good-1"
        assert resp.json()["status"] == "disabled"
        assert client.get("/v1/accounts", headers=admin_auth).status_code == 200
        assert client.delete("/v1/accounts/good-1", headers=admin_auth).status_code == 204


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
        assert resp.json()["error"]["code"] == "invalid_scope"

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
