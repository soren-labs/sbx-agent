import secrets

import pytest
from control.auth_store import AuthDatabase, AuthStore
from control.connections import ConnectionStore, SecretVault
from control.github_app import GitHubAppConfig, GitHubAppError
from control.real_github import GitHubFactory


class App:
    revoked = False
    suspended = False
    repos = ["owner/allowed", "owner/other"]

    def installation(self, iid):
        if self.revoked:
            raise GitHubAppError("not_found", "REDACTED", status_code=404)
        return {"id": iid, "account": {"login": "owner"}, "suspended_at": self.suspended}

    def create_installation_token(self, iid, repositories=None):
        self.last_scope = repositories
        return "REDACTED", 9999999999

    def installation_repositories(self, token):
        return "selected", self.repos


@pytest.fixture
def env(tmp_path):
    auth = AuthStore(AuthDatabase(path=tmp_path / "auth.db"))
    users = [auth.create_user() for _ in range(2)]
    store = ConnectionStore(auth, SecretVault(secrets.token_bytes(32)))
    app = App()
    factory = GitHubFactory(GitHubAppConfig("app", "test", "REDACTED"), app)
    return store, users, app, factory


def test_browser_cannot_claim_installation_without_operator_binding(env):
    store, users, _, factory = env
    service = factory(store, users[0].id)
    state = service.begin_authorization()["state"]
    with pytest.raises(GitHubAppError):
        service.complete_authorization(123, state)
    assert service.status()["installations"] == []


def test_bound_repo_scope_restart_cross_user_and_local_disconnect(env):
    store, users, app, factory = env
    factory.bind_installation(store, users[0].id, 123, ["owner/allowed"])
    service = factory(store, users[0].id)
    assert service.status()["installations"][0]["repositories"] == ["owner/allowed"]
    assert service.sandbox_token("owner/allowed") == "REDACTED"
    assert app.last_scope == ["allowed"]
    assert service.sandbox_token("owner/other") is None
    assert factory(store, users[1].id).sandbox_token("owner/allowed") is None
    assert factory(store, users[0].id).sandbox_token("owner/allowed") == "REDACTED"
    service.revoke(123)
    assert factory(store, users[0].id).sandbox_token("owner/allowed") is None


def test_upstream_uninstall_repo_removal_and_suspension_are_observed(env):
    store, users, app, factory = env
    factory.bind_installation(store, users[0].id, 123, ["owner/allowed"])
    app.repos = ["owner/other"]
    assert factory(store, users[0].id).sandbox_token("owner/allowed") is None
    app.repos = ["owner/allowed"]
    app.suspended = True
    assert factory(store, users[0].id).sandbox_token("owner/allowed") is None
    app.revoked = True
    assert factory(store, users[0].id).status()["installations"] == []


def test_operator_cannot_grant_repo_missing_from_live_installation(env):
    store, users, _, factory = env
    with pytest.raises(GitHubAppError):
        factory.bind_installation(store, users[0].id, 123, ["owner/missing"])
    assert factory(store, users[0].id).status()["installations"] == []


def test_app_jwt_window_has_skew_margin_and_provider_body_never_escapes():
    import httpx
    import jwt
    from control.real_github import SafeAppClient
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = SafeAppClient(
        GitHubAppConfig("app", "test", key),
        clock=lambda: 1000,
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text="REDACTED")),
    )
    claims = jwt.decode(client._jwt(), options={"verify_signature": False})
    assert claims["iat"] == 940 and claims["exp"] == 1540
    with pytest.raises(GitHubAppError) as error:
        client.installation(123)
    assert error.value.status_code == 401
    assert "REDACTED" not in str(error.value)
