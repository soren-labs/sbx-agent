"""SOR-177: GitHub App one-click authorization — config, stores, the REST
client (httpx MockTransport), the authorize/callback/sync/revoke service,
and the ``control.github`` injection seam.

Deterministic and cloud-free: the GitHub API is faked with
``httpx.MockTransport`` or a duck-typed client; the RSA key is generated
in-test and only ever materializes tokens like ``ghs_fake_*``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from control import github, github_app
from control.github_app import (
    FileGitHubAppStore,
    GitHubAppClient,
    GitHubAppConfig,
    GitHubAppError,
    GitHubAppService,
    InMemoryGitHubAppStore,
    InstallationRecord,
    ModalDictGitHubAppStore,
)

APP_ID = "424242"
APP_SLUG = "sbx-test-app"
MINTED = "ghs_fake_installation_token"  # test placeholder — not a real token


@pytest.fixture(scope="module")
def app_private_key() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


@pytest.fixture()
def config(app_private_key: str) -> GitHubAppConfig:
    return GitHubAppConfig(app_id=APP_ID, slug=APP_SLUG, private_key=app_private_key)


def make_record(
    iid: int = 7,
    *,
    login: str = "octo",
    selection: str = "selected",
    repos: list[str] | None = None,
    suspended: bool = False,
) -> InstallationRecord:
    return InstallationRecord(
        installation_id=iid,
        account_login=login,
        account_type="Organization",
        repository_selection=selection,
        repositories=repos if repos is not None else ["octo/hello"],
        suspended=suspended,
        recorded_at="2026-01-01T00:00:00+00:00",
        synced_at="2026-01-01T00:00:00+00:00",
    )


def installation_payload(
    iid: int = 7, *, login: str = "octo", selection: str = "selected"
) -> dict[str, Any]:
    return {
        "id": iid,
        "account": {"login": login, "type": "Organization"},
        "repository_selection": selection,
    }


# --------------------------------------------------------------------------
# config


class TestConfig:
    def test_empty_is_unconfigured(self) -> None:
        cfg = GitHubAppConfig.from_env({})
        assert not cfg.configured
        assert not cfg.installable

    def test_id_and_key_configured(self, app_private_key: str) -> None:
        cfg = GitHubAppConfig.from_env(
            {
                "SBX_GITHUB_APP_ID": APP_ID,
                "SBX_GITHUB_APP_PRIVATE_KEY": app_private_key,
            }
        )
        assert cfg.configured
        assert not cfg.installable  # slug needed to render the install URL

    def test_full_env_installable(self, app_private_key: str) -> None:
        cfg = GitHubAppConfig.from_env(
            {
                "SBX_GITHUB_APP_ID": f" {APP_ID} ",
                "SBX_GITHUB_APP_SLUG": "SBX-Test-App",
                "SBX_GITHUB_APP_PRIVATE_KEY": app_private_key.replace("\n", "\\n"),
            }
        )
        assert cfg.installable
        assert cfg.slug == "sbx-test-app"
        assert "\n" in cfg.private_key  # \n escapes normalized back to PEM


# --------------------------------------------------------------------------
# record semantics


class TestRecord:
    def test_selected_authorizes_listed_repo(self) -> None:
        rec = make_record(repos=["octo/hello", "octo/world"])
        assert rec.authorizes("octo/hello")
        assert rec.authorizes("OCTO/Hello")  # case-insensitive
        assert not rec.authorizes("octo/other")
        assert not rec.authorizes("other/hello")

    def test_all_covers_account_repos(self) -> None:
        rec = make_record(selection="all", repos=[])
        assert rec.authorizes("octo/anything")
        assert not rec.authorizes("other/repo")

    def test_suspended_authorizes_nothing(self) -> None:
        rec = make_record(repos=["octo/hello"], suspended=True)
        assert not rec.authorizes("octo/hello")

    def test_public_has_no_secret_fields(self) -> None:
        pub = make_record().public()
        assert pub["installation_id"] == 7
        for key in pub:
            assert "token" not in key
            assert "key" not in key
            assert "secret" not in key


# --------------------------------------------------------------------------
# stores


@pytest.fixture(
    params=["memory", "file"],
    ids=["InMemoryGitHubAppStore", "FileGitHubAppStore"],
)
def store(request: Any, tmp_path: Path) -> Any:
    if request.param == "memory":
        return InMemoryGitHubAppStore()
    return FileGitHubAppStore(tmp_path / "github-app")


class TestStore:
    def test_put_get_list_delete(self, store: Any) -> None:
        assert store.list() == []
        assert store.get(7) is None
        store.put(make_record(7, repos=["octo/hello"]))
        store.put(make_record(9, repos=["octo/world"]))
        assert [r.installation_id for r in store.list()] == [7, 9]
        assert store.get(7).repositories == ["octo/hello"]
        store.put(make_record(7, repos=["octo/hello", "octo/more"]))
        assert store.get(7).repositories == ["octo/hello", "octo/more"]
        store.delete(7)
        assert store.get(7) is None
        assert [r.installation_id for r in store.list()] == [9]

    def test_state_is_single_use(self, store: Any) -> None:
        store.put_state("st-1", 1234.5)
        assert store.pop_state("st-1") == 1234.5
        assert store.pop_state("st-1") is None
        assert store.pop_state("never-set") is None

    def test_file_store_survives_reopen(self, tmp_path: Path) -> None:
        root = tmp_path / "reopen"
        one = FileGitHubAppStore(root)
        one.put(make_record(7))
        one.put_state("st-9", 99.0)
        two = FileGitHubAppStore(root)
        assert two.get(7).account_login == "octo"
        assert two.pop_state("st-9") == 99.0


# --------------------------------------------------------------------------
# GitHub API client (httpx MockTransport)


def _request_log() -> list[httpx.Request]:
    return []


class TestClientJWT:
    def test_jwt_claims_and_signature(self, config: GitHubAppConfig, app_private_key: str) -> None:
        requests = _request_log()

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=[])

        client = GitHubAppClient(config, transport=httpx.MockTransport(handler))
        assert client.list_installations() == []
        auth = requests[0].headers["Authorization"]
        assert auth.startswith("Bearer ")
        token = auth.removeprefix("Bearer ")
        from cryptography.hazmat.primitives import serialization

        pub = serialization.load_pem_private_key(
            app_private_key.encode(), password=None
        ).public_key()
        decoded = jwt.decode(token, pub, algorithms=["RS256"], issuer=APP_ID)
        assert decoded["iss"] == APP_ID
        assert decoded["exp"] - decoded["iat"] == 660
        assert requests[0].headers["X-GitHub-Api-Version"] == "2022-11-28"


class TestClientCalls:
    def _client(self, config: GitHubAppConfig, handler: Any) -> GitHubAppClient:
        return GitHubAppClient(
            config, api_url="https://api.test", transport=httpx.MockTransport(handler)
        )

    def test_create_installation_token(self, config: GitHubAppConfig) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content or b"{}")
            return httpx.Response(201, json={"token": MINTED, "expires_at": "2030-01-01T00:00:00Z"})

        client = self._client(config, handler)
        token, expiry = client.create_installation_token(7, repositories=["hello"])
        assert token == MINTED
        assert expiry > time.time()
        assert seen["url"] == "https://api.test/app/installations/7/access_tokens"
        assert seen["body"] == {"repositories": ["hello"]}

    def test_create_installation_token_installation_wide(self, config: GitHubAppConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"token": MINTED, "expires_at": "2030-01-01T00:00:00Z"})

        client = self._client(config, handler)
        token, _ = client.create_installation_token(7)
        assert token == MINTED

    def test_list_installations_paginated(self, config: GitHubAppConfig) -> None:
        """``GET /app/installations`` defaults to 30 rows — the callback's
        membership check needs every page, so the client must walk
        ``per_page=100&page=N`` until a short page."""
        seen_pages: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            page = request.url.params.get("page", "1")
            assert request.url.params.get("per_page") == "100"
            seen_pages.append(page)
            if page == "1":
                return httpx.Response(200, json=[{"id": i} for i in range(100)])
            return httpx.Response(200, json=[{"id": 999}])

        client = self._client(config, handler)
        installs = client.list_installations()
        assert seen_pages == ["1", "2"]
        assert len(installs) == 101
        assert installs[-1]["id"] == 999

    def test_installation_repositories_paginated(self, config: GitHubAppConfig) -> None:
        pages = {
            "1": {
                "total_count": 2,
                "repository_selection": "selected",
                "repositories": [{"full_name": "octo/a"}],
            },
            "2": {
                "total_count": 2,
                "repository_selection": "selected",
                "repositories": [{"full_name": "octo/b"}],
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            page = request.url.params.get("page", "1")
            return httpx.Response(200, json=pages[page])

        client = self._client(config, handler)
        selection, repos = client.installation_repositories(MINTED)
        assert selection == "selected"
        assert repos == ["octo/a", "octo/b"]

    def test_delete_installation(self, config: GitHubAppConfig) -> None:
        def ok(request: httpx.Request) -> httpx.Response:
            return httpx.Response(204)

        def fail(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        assert self._client(config, ok).delete_installation(7) is True
        assert self._client(config, fail).delete_installation(7) is False

    def test_upstream_error_clipped_and_secret_free(self, config: GitHubAppConfig) -> None:
        body = "x" * 500 + APP_ID

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text=body)

        client = self._client(config, handler)
        with pytest.raises(GitHubAppError) as exc:
            client.list_installations()
        assert exc.value.code == "github_app_upstream"
        assert exc.value.status_code == 502
        assert APP_ID not in str(exc.value)
        assert len(str(exc.value)) < 300

    def test_transport_error_is_upstream(self, config: GitHubAppConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        client = self._client(config, handler)
        with pytest.raises(GitHubAppError) as exc:
            client.list_installations()
        assert exc.value.code == "github_app_upstream"
        assert "boom" not in str(exc.value)


# --------------------------------------------------------------------------
# service — fake client


class FakeClient:
    """Duck-typed ``GitHubAppClient`` for service tests."""

    def __init__(self) -> None:
        self.installations: list[dict[str, Any]] = []
        self.repos: dict[int, list[str]] = {}
        self.token_calls: list[tuple[int, list[str] | None]] = []
        self.deleted: list[int] = []
        self.delete_ok = True
        self._n = 0

    def list_installations(self) -> list[dict[str, Any]]:
        return list(self.installations)

    def create_installation_token(
        self, installation_id: int, *, repositories: list[str] | None = None
    ) -> tuple[str, float]:
        self._n += 1
        self.token_calls.append((installation_id, repositories))
        return f"{MINTED}_{self._n}", time.time() + 3600

    def installation_repositories(self, token: str) -> tuple[str, list[str]]:
        return "selected", self.repos.get(7, ["octo/hello"])

    def delete_installation(self, installation_id: int) -> bool:
        self.deleted.append(installation_id)
        return self.delete_ok


def make_service(
    config: GitHubAppConfig,
) -> tuple[GitHubAppService, FakeClient, InMemoryGitHubAppStore, list[float]]:
    client = FakeClient()
    store = InMemoryGitHubAppStore()
    now = [1_700_000_000.0]
    service = GitHubAppService(config, store, client, clock=lambda: now[0], records_ttl_s=0)
    return service, client, store, now


class TestAuthorizationFlow:
    def test_begin_requires_slug(self, config: GitHubAppConfig) -> None:
        service, _, _, _ = make_service(config)
        out = service.begin_authorization()
        assert out["authorize_url"].startswith(
            f"https://github.com/apps/{APP_SLUG}/installations/new?state="
        )
        assert out["state"] in out["authorize_url"]
        assert out["expires_at"]

    def test_begin_unconfigured(self, app_private_key: str) -> None:
        service, _, _, _ = make_service(GitHubAppConfig(app_id="", slug="", private_key=""))
        with pytest.raises(GitHubAppError) as exc:
            service.begin_authorization()
        assert exc.value.code == "github_app_unconfigured"
        assert exc.value.status_code == 503
        # configured but no slug cannot render the install URL either
        service2, _, _, _ = make_service(
            GitHubAppConfig(app_id=APP_ID, slug="", private_key=app_private_key)
        )
        with pytest.raises(GitHubAppError):
            service2.begin_authorization()

    def test_callback_records_selected_repo_metadata(self, config: GitHubAppConfig) -> None:
        service, client, store, _ = make_service(config)
        client.installations = [installation_payload(7)]
        client.repos[7] = ["octo/hello", "octo/world"]
        out = service.begin_authorization()
        record = service.complete_authorization(7, out["state"])
        assert record.installation_id == 7
        assert record.account_login == "octo"
        assert record.repository_selection == "selected"
        assert record.repositories == ["octo/hello", "octo/world"]
        assert store.get(7) is not None
        # The token minted to enumerate repos was used in-line only — never
        # persisted on the record.
        assert MINTED not in repr(record.public())
        # ``state`` is single-use — replaying it fails.
        with pytest.raises(GitHubAppError) as exc:
            service.complete_authorization(7, out["state"])
        assert exc.value.code == "github_app_state"
        assert exc.value.status_code == 403

    def test_callback_rejects_bad_state_and_id(self, config: GitHubAppConfig) -> None:
        service, client, _, _ = make_service(config)
        client.installations = [installation_payload(7)]
        service.begin_authorization()
        with pytest.raises(GitHubAppError) as exc:
            service.complete_authorization(7, "wrong-state")
        assert exc.value.code == "github_app_state"
        with pytest.raises(GitHubAppError) as exc:
            service.complete_authorization(7, None)
        assert exc.value.code == "github_app_state"
        fresh = service.begin_authorization()
        with pytest.raises(GitHubAppError) as exc:
            service.complete_authorization("notanint", fresh["state"])
        assert exc.value.code == "github_app_invalid"

    def test_callback_expired_state(self, config: GitHubAppConfig) -> None:
        service, client, store, now = make_service(config)
        client.installations = [installation_payload(7)]
        out = service.begin_authorization()
        now[0] += github_app.AUTHORIZE_STATE_TTL_S + 1
        with pytest.raises(GitHubAppError) as exc:
            service.complete_authorization(7, out["state"])
        assert exc.value.code == "github_app_state"
        # The expired state was consumed — a store-clean retry also fails.
        assert store.pop_state(out["state"]) is None

    def test_callback_unknown_installation_is_404(self, config: GitHubAppConfig) -> None:
        service, client, _, _ = make_service(config)
        client.installations = [installation_payload(8)]
        out = service.begin_authorization()
        with pytest.raises(GitHubAppError) as exc:
            service.complete_authorization(7, out["state"])
        assert exc.value.code == "not_found"


class TestSyncAndRevoke:
    def test_sync_refreshes_and_drops_vanished(self, config: GitHubAppConfig) -> None:
        service, client, store, _ = make_service(config)
        store.put(make_record(7, repos=["octo/hello"]))
        store.put(make_record(9, repos=["octo/gone"]))
        client.installations = [installation_payload(7)]
        client.repos[7] = ["octo/hello", "octo/new"]
        out = service.sync()
        assert [r.installation_id for r in out] == [7]
        assert store.get(7).repositories == ["octo/hello", "octo/new"]
        assert store.get(9) is None  # deleted upstream → dropped locally

    def test_sync_unconfigured(self) -> None:
        service, _, _, _ = make_service(GitHubAppConfig())
        with pytest.raises(GitHubAppError) as exc:
            service.sync()
        assert exc.value.code == "github_app_unconfigured"

    def test_revoke_one_clears_record_and_tokens(self, config: GitHubAppConfig) -> None:
        service, client, store, _ = make_service(config)
        store.put(make_record(7))
        assert service.sandbox_token(repo="octo/hello") is not None
        out = service.revoke(7)
        assert out == {"revoked": 1, "remote_deleted": True}
        assert client.deleted == [7]
        assert store.get(7) is None
        assert service.sandbox_token(repo="octo/hello") is None

    def test_revoke_unknown_is_404(self, config: GitHubAppConfig) -> None:
        service, client, _, _ = make_service(config)
        with pytest.raises(GitHubAppError) as exc:
            service.revoke(999)
        assert exc.value.code == "not_found"
        assert client.deleted == []

    def test_revoke_forgets_locally_when_upstream_fails(self, config: GitHubAppConfig) -> None:
        service, client, store, _ = make_service(config)
        store.put(make_record(7))
        client.delete_ok = False
        out = service.revoke(7)
        assert out == {"revoked": 1, "remote_deleted": False}
        assert store.get(7) is None  # fail-closed: never keeps working

    def test_revoke_all(self, config: GitHubAppConfig) -> None:
        service, client, store, _ = make_service(config)
        store.put(make_record(7))
        store.put(make_record(9))
        out = service.revoke()
        assert out == {"revoked": 2, "remote_deleted": True}
        assert sorted(client.deleted) == [7, 9]
        assert store.list() == []


class TestSandboxTokenSeam:
    def test_repo_scoped_mint_and_cache(self, config: GitHubAppConfig) -> None:
        service, client, store, _ = make_service(config)
        store.put(make_record(7, repos=["octo/hello"]))
        token = service.sandbox_token(repo="https://github.com/octo/hello")
        assert token is not None and token.startswith(MINTED)
        assert client.token_calls[-1] == (7, ["hello"])
        # cached — no second mint within the expiry margin
        assert service.sandbox_token(repo="octo/hello") == token
        assert len(client.token_calls) == 1

    def test_mint_refreshes_near_expiry(self, config: GitHubAppConfig) -> None:
        client = FakeClient()
        store = InMemoryGitHubAppStore()
        now = [1_700_000_000.0]
        calls: list[Any] = []

        def mint(iid: int, *, repositories: list[str] | None = None) -> tuple[str, float]:
            calls.append((iid, repositories))
            return f"{MINTED}_{len(calls)}", now[0] + 3600

        client.create_installation_token = mint  # type: ignore[assignment]
        service = GitHubAppService(config, store, client, clock=lambda: now[0], records_ttl_s=0)
        store.put(make_record(7))
        first = service.sandbox_token(repo="octo/hello")
        assert service.sandbox_token(repo="octo/hello") == first
        assert len(calls) == 1  # cached under expiry
        now[0] += 3600 - 60  # inside the 120 s refresh margin
        second = service.sandbox_token(repo="octo/hello")
        assert second != first
        assert len(calls) == 2

    def test_unauthorized_repo_fails_closed(self, config: GitHubAppConfig) -> None:
        service, client, store, _ = make_service(config)
        store.put(make_record(7, repos=["octo/hello"]))
        assert service.sandbox_token(repo="octo/other") is None
        assert service.sandbox_token(repo="https://gitlab.com/octo/hello") is None
        assert client.token_calls == []  # never minted

    def test_no_repo_uses_first_active_installation(self, config: GitHubAppConfig) -> None:
        service, client, store, _ = make_service(config)
        store.put(make_record(7, suspended=True))
        store.put(make_record(9, repos=["octo/world"]))
        token = service.sandbox_token()
        assert token is not None
        assert client.token_calls[-1] == (9, None)  # installation-wide mint

    def test_unconfigured_or_empty_supply_nothing(self, app_private_key: str) -> None:
        service, client, _, _ = make_service(GitHubAppConfig())
        assert service.sandbox_token() is None
        assert service.can_supply() is False
        service2, client2, _, _ = make_service(
            GitHubAppConfig(app_id=APP_ID, slug=APP_SLUG, private_key=app_private_key)
        )
        assert service2.sandbox_token() is None  # no installations recorded
        assert service2.can_supply() is False
        assert client2.token_calls == []

    def test_can_supply_repo_scoping(self, config: GitHubAppConfig) -> None:
        service, _, store, _ = make_service(config)
        store.put(make_record(7, repos=["octo/hello"]))
        assert service.can_supply() is True
        assert service.can_supply(repo="octo/hello") is True
        assert service.can_supply(repo="https://github.com/octo/hello") is True
        assert service.can_supply(repo="octo/other") is False
        # A non-github.com repo authorizes nothing — fail closed.
        assert service.can_supply(repo="/local/path") is False

    def test_installation_for_repo_owner_match(self, config: GitHubAppConfig) -> None:
        service, _, store, _ = make_service(config)
        store.put(make_record(7, login="octo", selection="all", repos=[]))
        assert service.installation_for_repo("octo/any").installation_id == 7
        assert service.installation_for_repo("other/any") is None

    def test_status_is_metadata_only(self, config: GitHubAppConfig) -> None:
        service, _, store, _ = make_service(config)
        store.put(make_record(7))
        out = service.status()
        assert out["configured"] is True
        assert out["installable"] is True
        assert out["app_id"] == APP_ID
        assert out["app_slug"] == APP_SLUG
        assert out["bridge_token"] is False
        assert [i["installation_id"] for i in out["installations"]] == [7]
        rendered = json.dumps(out)
        assert config.private_key not in rendered
        assert "PRIVATE KEY" not in rendered


# --------------------------------------------------------------------------
# module seams + control.github integration


class TestModuleSeams:
    def test_default_service_memoized(self, app_private_key: str) -> None:
        github_app.reset_default_service()
        try:
            env = {
                "SBX_GITHUB_APP_ID": APP_ID,
                "SBX_GITHUB_APP_SLUG": APP_SLUG,
                "SBX_GITHUB_APP_PRIVATE_KEY": app_private_key,
            }
            one = github_app.default_service(env)
            assert github_app.default_service(env) is one
            other = github_app.default_service({**env, "SBX_GITHUB_APP_SLUG": "x"})
            assert other is not one
        finally:
            github_app.reset_default_service()

    def test_default_store_selects_file_vs_modal(
        self, app_private_key: str, tmp_path: Path
    ) -> None:
        github_app.reset_default_service()
        try:
            env = {
                "SBX_GITHUB_APP_ID": APP_ID,
                "SBX_GITHUB_APP_PRIVATE_KEY": app_private_key,
                "SBX_GITHUB_APP_STORE_DIR": str(tmp_path / "app"),
            }
            svc = github_app.default_service(env)
            assert svc.config.configured
            github_app.reset_default_service()
            modal_env = {**env, "SBX_BACKEND": "modal"}
            svc2 = github_app.default_service(modal_env)
            assert svc2 is not svc  # different store under modal
        finally:
            github_app.reset_default_service()

    def test_module_sandbox_token_fails_closed(self) -> None:
        github_app.reset_default_service()
        try:
            # Unconfigured env → no exception escapes, just None/False.
            assert github_app.sandbox_token({}) is None
            assert github_app.can_supply({}) is False
            posture = github_app.posture({})
            assert posture == {
                "configured": False,
                "installable": False,
                "app_id": None,
                "installations": 0,
            }
        finally:
            github_app.reset_default_service()


class TestGithubBridgeIntegration:
    """The App as a token source inside the SOR-117 opt-in seam."""

    def _app_env(self, app_private_key: str) -> dict[str, str]:
        return {
            "SBX_GITHUB_APP_ID": APP_ID,
            "SBX_GITHUB_APP_SLUG": APP_SLUG,
            "SBX_GITHUB_APP_PRIVATE_KEY": app_private_key,
            "SBX_GITHUB_APP_STORE_DIR": "",
        }

    def test_env_pat_still_wins_over_app(
        self, app_private_key: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(github_app, "can_supply", lambda *a, **k: True)
        called: list[Any] = []
        monkeypatch.setattr(github_app, "sandbox_token", lambda *a, **k: called.append(1) or MINTED)
        env = {"SBX_GITHUB_EPHEMERAL": "1", "GH_TOKEN": "env-pat"}
        assert github.token_source(env) == "GH_TOKEN"
        out = github.secret_env(env)
        assert out["GH_TOKEN"] == "env-pat"
        assert called == []  # env PAT never consults the App

    def test_app_supplies_when_no_env_token(
        self, app_private_key: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(github_app, "can_supply", lambda *a, **k: True)
        monkeypatch.setattr(github_app, "sandbox_token", lambda *a, **k: MINTED)
        env = {"SBX_GITHUB_EPHEMERAL": "1"}
        assert github.token_source(env) == "github_app"
        assert github.injection_enabled(env)
        out = github.secret_env(env)
        assert out == {"GH_TOKEN": MINTED, "GITHUB_TOKEN": MINTED}
        exec_out = github.exec_env(env)
        assert exec_out["GIT_CONFIG_COUNT"] == "2"

    def test_repo_context_flows_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[Any] = []

        def fake_can_supply(env: Any, *, repo: Any = None) -> bool:
            seen.append(("can_supply", repo))
            return True

        def fake_mint(env: Any, *, repo: Any = None) -> str:
            seen.append(("sandbox_token", repo))
            return MINTED

        monkeypatch.setattr(github_app, "can_supply", fake_can_supply)
        monkeypatch.setattr(github_app, "sandbox_token", fake_mint)
        env = {"SBX_GITHUB_EPHEMERAL": "1"}
        github.exec_env(env, repo="https://github.com/octo/hello")
        assert ("sandbox_token", "https://github.com/octo/hello") in seen
        assert ("can_supply", "https://github.com/octo/hello") in seen

    def test_gate_off_means_no_app_injection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(github_app, "can_supply", lambda *a, **k: True)
        monkeypatch.setattr(github_app, "sandbox_token", lambda *a, **k: MINTED)
        env: dict[str, str] = {}
        assert github.exec_env(env) == {}  # gate off → nothing injects
        assert github.token_source(env) == "github_app"  # source named anyway

    def test_mint_failure_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(github_app, "can_supply", lambda *a, **k: True)
        monkeypatch.setattr(
            github_app,
            "sandbox_token",
            lambda *a, **k: None,  # mint failed → nothing to inject
        )
        env = {"SBX_GITHUB_EPHEMERAL": "1"}
        assert github.secret_env(env) == {}
        assert github.exec_env(env)["GIT_CONFIG_COUNT"] == "2"  # wiring still on

    def test_detect_reports_app_posture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            github_app,
            "posture",
            lambda env: {
                "configured": True,
                "installable": True,
                "app_id": APP_ID,
                "installations": 2,
            },
        )
        det = github.detect({"SBX_GITHUB_EPHEMERAL": "1"}, which=lambda name: None)
        assert det.app_configured
        assert det.app_installations == 2
        assert det.detected


class TestModalDictStoreShape:
    """No modal connection — just the key layout the Dict uses."""

    def test_lazy_dict_and_key_prefixes(self) -> None:
        store = ModalDictGitHubAppStore("sbx-test-app-dict")
        backing: dict[str, Any] = {}

        class _FakeDict(dict):
            pass

        store._dict = _FakeDict(backing)
        store.put(make_record(7))
        store.put_state("st-1", 42.0)
        assert "installation:7" in store._dict
        assert "state:st-1" in store._dict
        assert store.get(7).installation_id == 7
        assert store.pop_state("st-1") == 42.0
        assert store.pop_state("st-1") is None
        assert [r.installation_id for r in store.list()] == [7]
        store.delete(7)
        assert store.get(7) is None
