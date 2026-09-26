"""SOR-220 default connect routes: ``POST /v1/github/install`` +
``GET /v1/github/install/callback``.

The SBX-side service and store are real (InMemory); the hosted broker is a
duck-typed fake. These prove: the default URL is the GitHub App
*installation* page (never settings/apps/new), the callback binds the
installation to this deployment, broker-minted tokens flow to the sandbox
seam, and no private key material ever crosses an API response.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from control.github_app import (
    GitHubAppConfig,
    GitHubAppService,
    InMemoryGitHubAppStore,
)
from control.github_broker import GitHubBrokerError

APP_SLUG = "sbx-public"


class FakeGitHub:
    """Upstream GitHub edge for deployment-local App flows (unused in
    broker mode but needed by the service constructor)."""

    def __init__(self) -> None:
        self.installations: list[dict[str, Any]] = []
        self.repos: dict[int, list[str]] = {}

    def list_installations(self) -> list[dict[str, Any]]:
        return list(self.installations)

    def create_installation_token(
        self, installation_id: int, *, repositories: list[str] | None = None
    ) -> tuple[str, float]:
        return "ghs_local_fake", time.time() + 3600

    def installation_repositories(self, token: str) -> tuple[str, list[str]]:
        return "selected", ["octo/hello"]

    def delete_installation(self, installation_id: int) -> bool:
        return True


class FakeBroker:
    """Duck-typed GitHubBrokerClient — records calls, no network. Models
    the real contract: origin registration first, then session creation
    authenticated by the deployment-scoped credential."""

    base_url = "https://broker.test"

    def __init__(self) -> None:
        self.registrations: list[str] = []
        self.sessions: list[tuple[str, str]] = []
        self.claimed: list[str] = []
        self.mints: list[tuple[int, str, list[str] | None]] = []
        self.synced: list[int] = []
        self.revoked: list[int] = []
        self.health_ok = True
        # Test knobs: fail one credential, lose deployments server-side.
        self.reject_credential: str | None = None
        self.registered: dict[str, str] = {}

    def register_deployment(self, origin: str) -> dict[str, Any]:
        self.registrations.append(origin)
        return {
            "registration_id": f"reg_{len(self.registrations)}",
            "challenge": "challenge_fake",
            "challenge_url": f"{origin}/.well-known/sbx-broker-challenge",
            "expires_at": "2026-01-01T00:10:00Z",
        }

    def complete_registration(self, registration_id: str) -> dict[str, Any]:
        origin = self.registrations[-1]
        credential = f"sbxdep_fake_{len(self.registrations)}"
        self.registered[origin] = credential
        return {
            "origin": origin,
            "credential": credential,
            "redirect_uri": f"{origin}/v1/github/install/callback",
        }

    def create_session(self, origin: str, credential: str) -> dict[str, Any]:
        self.sessions.append((origin, credential))
        if credential == self.reject_credential:
            raise GitHubBrokerError("broker_auth", "bad credential", status_code=403)
        return {
            "install_url": (
                f"https://github.com/apps/{APP_SLUG}/installations/new?state=sbk1.fakesig"
            ),
            "state": "sbk1.fakesig",
            "expires_at": "2026-01-01T00:10:00Z",
        }

    def claim(self, code: str) -> dict[str, Any]:
        self.claimed.append(code)
        if code != "sbxclaim_good":
            raise GitHubBrokerError("broker_claim", "unknown code", status_code=403)
        return {
            "installation": {
                "installation_id": 88,
                "account_login": "octo",
                "account_type": "Organization",
                "repository_selection": "selected",
                "repositories": ["octo/hello"],
                "suspended": False,
                "deployment": "https://sbx.example.test",
            },
            "credential": "sbxbrk_fake",  # placeholder, not real
        }

    def mint_token(
        self, installation_id: int, credential: str, *, repositories: list[str] | None = None
    ) -> tuple[str, float]:
        self.mints.append((installation_id, credential, repositories))
        return "ghs_broker_minted", time.time() + 3600  # placeholder, not real

    def sync_installation(self, installation_id: int, credential: str) -> dict[str, Any]:
        self.synced.append(installation_id)
        return {
            "installation": {
                "installation_id": installation_id,
                "account_login": "octo",
                "account_type": "Organization",
                "repository_selection": "selected",
                "repositories": ["octo/hello", "octo/world"],
                "suspended": False,
            }
        }

    def revoke_installation(self, installation_id: int, credential: str) -> dict[str, Any]:
        self.revoked.append(installation_id)
        return {"revoked": 1, "remote_deleted": True}

    def health(self) -> dict[str, Any]:
        return {"ok": self.health_ok, "configured": True, "app_slug": APP_SLUG}


@pytest.fixture(autouse=True)
def public_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the deployment's public https origin — a test client runs on
    ``http://testserver``, which is neither https nor loopback."""
    monkeypatch.setenv("SBX_PUBLIC_ORIGIN", "https://sbx.example.test")


@pytest.fixture()
def broker_service(v1_env: Any) -> FakeBroker:
    """Service with NO local App — the default self-hosted posture where
    the broker lane is the connect path."""
    broker = FakeBroker()
    service = GitHubAppService(
        GitHubAppConfig(), InMemoryGitHubAppStore(), FakeGitHub(), broker=broker
    )
    v1_env.app.state.github_app = service
    return broker


@pytest.fixture()
def app_mode_service(v1_env: Any, broker_service: FakeBroker) -> None:
    """A deployment-local App present: broker stays the fallback, not the
    default."""
    v1_env.app.state.github_app._env_config = GitHubAppConfig(
        app_id="424242", slug="sbx-self-app", private_key="fake-pem"
    )


def _service(v1_env: Any) -> GitHubAppService:
    return v1_env.app.state.github_app


class TestBeginInstall:
    def test_default_is_installations_new(
        self, client: Any, auth: dict, broker_service: FakeBroker
    ) -> None:
        resp = client.post("/v1/github/install", headers=auth)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["mode"] == "broker"
        assert body["authorize_url"].startswith(
            f"https://github.com/apps/{APP_SLUG}/installations/new"
        )
        # Acceptance gate: the first GitHub page must never be the
        # App-registration surface.
        assert "settings/apps/new" not in body["authorize_url"]
        # The flow was bound to THIS deployment's registered https origin —
        # registration proved origin control first, then session creation
        # presented the issued deployment credential.
        assert broker_service.registrations == ["https://sbx.example.test"]
        assert broker_service.sessions == [("https://sbx.example.test", "sbxdep_fake_1")]

    def test_requires_auth(self, client: Any, broker_service: FakeBroker) -> None:
        assert client.post("/v1/github/install").status_code == 401

    def test_local_app_prefers_app_mode(
        self, client: Any, auth: dict, broker_service: FakeBroker, app_mode_service: None
    ) -> None:
        """An operator-supplied App keeps the SOR-177 authorize flow — the
        broker is never invoked."""
        resp = client.post("/v1/github/install", headers=auth)
        assert resp.status_code == 201
        body = resp.json()
        assert body["mode"] == "app"
        assert "/apps/sbx-self-app/installations/new" in body["authorize_url"]
        assert broker_service.sessions == []

    def test_no_app_no_broker_is_503(self, client: Any, auth: dict, v1_env: Any) -> None:
        v1_env.app.state.github_app = GitHubAppService(
            GitHubAppConfig(), InMemoryGitHubAppStore(), FakeGitHub(), broker=None
        )
        resp = client.post("/v1/github/install", headers=auth)
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "github_app_unconfigured"

    def test_stale_credential_triggers_reregistration(
        self, client: Any, auth: dict, broker_service: FakeBroker
    ) -> None:
        """A wiped broker-side deployment record rejects the stored
        credential once; the service re-proves origin control and retries
        — the user still gets a one-click install URL."""
        broker_service.reject_credential = "sbxdep_fake_1"
        resp = client.post("/v1/github/install", headers=auth)
        assert resp.status_code == 201, resp.text
        assert len(broker_service.registrations) == 2
        assert broker_service.sessions[-1] == (
            "https://sbx.example.test",
            "sbxdep_fake_2",
        )

    def test_http_origin_is_rejected_before_broker(
        self,
        client: Any,
        auth: dict,
        broker_service: FakeBroker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fail closed: a non-public (plain http, non-loopback) origin can
        never reach the broker's session API."""
        monkeypatch.setenv("SBX_PUBLIC_ORIGIN", "http://insecure.example.test")
        resp = client.post("/v1/github/install", headers=auth)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "github_app_invalid"
        assert broker_service.sessions == []


class TestCallback:
    def test_binds_installation_to_deployment(
        self, client: Any, auth: dict, broker_service: FakeBroker
    ) -> None:
        resp = client.get(
            "/v1/github/install/callback",
            params={"code": "sbxclaim_good"},
            headers={},  # deliberately unauthenticated — the code is the credential
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/#/admin/github?broker=connected"
        assert broker_service.claimed == ["sbxclaim_good"]

        status = client.get("/v1/github/app", headers=auth).json()
        assert status["configured"] is True
        assert status["source"] == "broker"
        assert status["broker"]["bound"] is True
        inst = status["installations"][0]
        assert inst["installation_id"] == 88
        assert inst["via"] == "broker"
        # The broker credential never surfaces in API JSON.
        assert "sbxbrk_fake" not in resp.text and "sbxbrk_fake" not in str(status)

    def test_bad_code_redirects_with_error_and_binds_nothing(
        self, client: Any, auth: dict, broker_service: FakeBroker
    ) -> None:
        resp = client.get(
            "/v1/github/install/callback",
            params={"code": "sbxclaim_replayed"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/#/admin/github?broker_error=")
        status = client.get("/v1/github/app", headers=auth).json()
        assert status["installations"] == []
        assert status["configured"] is False


class TestBrokeredTokenPath:
    def _connect(self, client: Any) -> None:
        client.get(
            "/v1/github/install/callback",
            params={"code": "sbxclaim_good"},
            follow_redirects=False,
        )

    def test_sync_drops_revoked_broker_installation(
        self, client: Any, auth: dict, admin_auth: dict, broker_service: FakeBroker
    ) -> None:
        """Finding 2: when the broker reports the installation gone, sync
        must delete the local record + binding and never serialize a None
        row (the old None-append 500)."""
        self._connect(client)

        def gone(installation_id: int, credential: str) -> dict[str, Any]:
            raise GitHubBrokerError("not_found", "installation gone", status_code=404)

        broker_service.sync_installation = gone  # type: ignore[assignment]
        resp = client.post("/v1/github/app/sync", headers=admin_auth)
        assert resp.status_code == 200, resp.text
        assert resp.json()["installations"] == []
        status = client.get("/v1/github/app", headers=auth).json()
        assert status["installations"] == []
        assert status["configured"] is False

    def test_sandbox_token_mints_via_broker_least_privilege(
        self, client: Any, auth: dict, broker_service: FakeBroker, v1_env: Any
    ) -> None:
        """Private-repo token path: the deployment mints through the broker,
        narrowed to the target repo — the App private key is never here."""
        self._connect(client)
        token = _service(v1_env).sandbox_token("octo/hello")
        assert token == "ghs_broker_minted"
        assert broker_service.mints == [(88, "sbxbrk_fake", ["hello"])]

    def test_sync_and_revoke_route_through_broker(
        self, client: Any, auth: dict, admin_auth: dict, broker_service: FakeBroker
    ) -> None:
        self._connect(client)
        resp = client.post("/v1/github/app/sync", headers=admin_auth)
        assert resp.status_code == 200
        assert broker_service.synced == [88]
        inst = resp.json()["installations"][0]
        assert inst["repositories"] == ["octo/hello", "octo/world"]

        resp = client.delete("/v1/github/app/installations/88", headers=admin_auth)
        assert resp.status_code == 200
        assert resp.json()["remote_deleted"] is True
        assert broker_service.revoked == [88]
        assert client.get("/v1/github/app", headers=auth).json()["installations"] == []
