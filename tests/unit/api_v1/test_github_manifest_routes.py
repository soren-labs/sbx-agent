"""SOR-220: GitHub App Manifest zero-config routes — /v1/github/app/manifest*.

Cloud-free: the GitHub API is a duck-typed fake client; registered App
material stays in the injected InMemory store, and every response is
checked for private-material leakage.
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

APP_PEM_FIXTURE = "PEM-PLACEHOLDER"


class ManifestClient:
    def __init__(self, pem: str = APP_PEM_FIXTURE) -> None:
        self.pem = pem
        self.conversions: list[str] = []

    def exchange_manifest_code(self, code: str) -> dict[str, Any]:
        self.conversions.append(code)
        return {
            "id": 55001,
            "slug": "sbx-manifest-app",
            "client_id": "Iv1.fake",
            "client_secret": "fake-client-secret",
            "pem": self.pem,
            "webhook_secret": "fake-webhook-secret",
            "name": "sbx-manifest-app",
            "html_url": "https://github.com/apps/sbx-manifest-app",
        }

    def list_installations(self) -> list[dict[str, Any]]:
        return []

    def create_installation_token(
        self, installation_id: int, *, repositories: list[str] | None = None
    ) -> tuple[str, float]:
        return "ghs_fake_installation_token", time.time() + 3600

    def installation_repositories(self, token: str) -> tuple[str, list[str]]:
        return "selected", []

    def delete_installation(self, installation_id: int) -> bool:
        return True


class RecordingSecretWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def refresh(self, name: str, env: dict[str, str]) -> None:
        self.calls.append((name, env))


@pytest.fixture()
def manifest_service(v1_env: Any) -> GitHubAppService:
    """Unconfigured deployment (no env App) with a fake GitHub client."""
    service = GitHubAppService(
        GitHubAppConfig(),
        InMemoryGitHubAppStore(),
        ManifestClient(),
        secret_writer=RecordingSecretWriter(),
        secret_name="sbx-github-app",
    )
    v1_env.app.state.github_app = service
    return service


class TestBegin:
    def test_begin_returns_manifest_and_url(
        self, client: Any, admin_auth: dict, manifest_service: GitHubAppService
    ) -> None:
        resp = client.post("/v1/github/app/manifest", json={}, headers=admin_auth)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["manifest_url"].startswith("https://github.com/settings/apps/new?state=")
        manifest = body["manifest"]
        assert manifest["redirect_url"].endswith("/v1/github/app/manifest/callback")
        assert manifest["default_permissions"]["contents"] == "write"
        assert body["state"] in body["manifest_url"]
        # No private material anywhere in step 1.
        for needle in ("pem", "client_secret", "private_key"):
            assert needle not in resp.text

    def test_begin_requires_admin(
        self, client: Any, auth: dict, manifest_service: GitHubAppService
    ) -> None:
        assert client.post("/v1/github/app/manifest", json={}, headers=auth).status_code == 403
        assert client.post("/v1/github/app/manifest", json={}).status_code == 401

    def test_begin_conflict_when_configured(
        self, client: Any, admin_auth: dict, v1_env: Any, manifest_service: GitHubAppService
    ) -> None:
        # A second begin with an already-registered app is a 409.
        client.post("/v1/github/app/manifest", json={}, headers=admin_auth)
        begun = client.post("/v1/github/app/manifest", json={}, headers=admin_auth)
        assert begun.status_code == 201
        body = begun.json()
        client.get(
            f"/v1/github/app/manifest/callback?code=c1&state={body['state']}",
            follow_redirects=False,
        )
        resp = client.post("/v1/github/app/manifest", json={}, headers=admin_auth)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "github_app_configured"


class TestComplete:
    def _begin(self, client: Any, admin_auth: dict) -> str:
        resp = client.post("/v1/github/app/manifest", json={}, headers=admin_auth)
        assert resp.status_code == 201, resp.text
        return resp.json()["state"]

    def test_callback_registers_and_redirects(
        self, client: Any, admin_auth: dict, manifest_service: GitHubAppService
    ) -> None:
        state = self._begin(client, admin_auth)
        resp = client.get(
            f"/v1/github/app/manifest/callback?code=conv-1&state={state}",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/#/admin/github?manifest=connected"
        # The app is now configured from the registry lane — usable
        # immediately (authorize works without a redeploy).
        status = client.get("/v1/github/app", headers=admin_auth).json()
        assert status["configured"] is True
        assert status["source"] == "registry"
        assert status["app_url"] == "https://github.com/apps/sbx-manifest-app"
        auth = client.post("/v1/github/app/authorize", json={}, headers=admin_auth)
        assert auth.status_code == 201, auth.text
        assert "sbx-manifest-app" in auth.json()["authorize_url"]
        # No private material on any read surface.
        assert "fake-client-secret" not in client.get("/v1/github/app", headers=admin_auth).text

    def test_callback_needs_no_auth(
        self, client: Any, admin_auth: dict, manifest_service: GitHubAppService
    ) -> None:
        state = self._begin(client, admin_auth)
        resp = client.get(
            f"/v1/github/app/manifest/callback?code=conv-1&state={state}",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "manifest=connected" in resp.headers["location"]

    def test_callback_bad_state_redirects_with_error(
        self, client: Any, manifest_service: GitHubAppService
    ) -> None:
        resp = client.get(
            "/v1/github/app/manifest/callback?code=x&state=bogus",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "manifest_error=github_app_state" in resp.headers["location"]

    def test_complete_endpoint_registers(
        self, client: Any, admin_auth: dict, manifest_service: GitHubAppService
    ) -> None:
        state = self._begin(client, admin_auth)
        resp = client.post(
            "/v1/github/app/manifest/complete",
            json={"code": "conv-2", "state": state},
            headers=admin_auth,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["slug"] == "sbx-manifest-app"
        assert body["source"] == "registry"
        assert body["secret"] == "refreshed"
        for needle in (APP_PEM_FIXTURE, "fake-client-secret", "fake-webhook-secret"):
            assert needle not in resp.text

    def test_complete_endpoint_rejects_bad_state(
        self, client: Any, admin_auth: dict, manifest_service: GitHubAppService
    ) -> None:
        resp = client.post(
            "/v1/github/app/manifest/complete",
            json={"code": "conv-2", "state": "bogus"},
            headers=admin_auth,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "github_app_state"
