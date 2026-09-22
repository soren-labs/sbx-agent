"""SOR-177: ``/v1/github/app*`` endpoints — one-click authorize, callback,
status, sync, revoke. The service is real (InMemory store + duck-typed
client); only the GitHub HTTP edge is faked.
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


class FakeClient:
    def __init__(self) -> None:
        self.installations: list[dict[str, Any]] = []
        self.repos: dict[int, list[str]] = {}
        self.deleted: list[int] = []
        self._n = 0

    def list_installations(self) -> list[dict[str, Any]]:
        return list(self.installations)

    def create_installation_token(
        self, installation_id: int, *, repositories: list[str] | None = None
    ) -> tuple[str, float]:
        self._n += 1
        return f"{MINTED}_{self._n}", time.time() + 3600

    def installation_repositories(self, token: str) -> tuple[str, list[str]]:
        return "selected", self.repos.get(7, ["octo/hello"])

    def delete_installation(self, installation_id: int) -> bool:
        self.deleted.append(installation_id)
        return True


@pytest.fixture()
def app_service(v1_env: Any, app_private_key: str) -> FakeClient:
    """Inject a real service (InMemory store + fake GitHub client)."""
    client = FakeClient()
    service = GitHubAppService(
        GitHubAppConfig(app_id=APP_ID, slug=APP_SLUG, private_key=app_private_key),
        InMemoryGitHubAppStore(),
        client,
    )
    v1_env.app.state.github_app = service
    return client


@pytest.fixture()
def unconfigured_service(v1_env: Any) -> None:
    v1_env.app.state.github_app = GitHubAppService(
        GitHubAppConfig(), InMemoryGitHubAppStore(), FakeClient()
    )


class TestStatus:
    def test_posture(self, client: Any, auth: dict, app_service: FakeClient) -> None:
        resp = client.get("/v1/github/app", headers=auth)
        assert resp.status_code == 200
        body = resp.json()
        assert body["configured"] is True
        assert body["installable"] is True
        assert body["app_slug"] == APP_SLUG
        assert body["installations"] == []
        assert body["bridge_token"] is False
        assert "PRIVATE KEY" not in resp.text
        assert MINTED not in resp.text

    def test_requires_auth(self, client: Any, app_service: FakeClient) -> None:
        assert client.get("/v1/github/app").status_code == 401

    def test_unconfigured_status(self, client: Any, auth: dict, unconfigured_service: None) -> None:
        body = client.get("/v1/github/app", headers=auth).json()
        assert body["configured"] is False
        assert body["installations"] == []


class TestAuthorize:
    def test_begin_returns_install_url(
        self, client: Any, auth: dict, app_service: FakeClient
    ) -> None:
        resp = client.post("/v1/github/app/authorize", headers=auth)
        assert resp.status_code == 201
        body = resp.json()
        assert body["authorize_url"].startswith(
            f"https://github.com/apps/{APP_SLUG}/installations/new?state="
        )
        assert body["state"]
        assert body["expires_at"]

    def test_unconfigured_is_503(self, client: Any, auth: dict, unconfigured_service: None) -> None:
        resp = client.post("/v1/github/app/authorize", headers=auth)
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "github_app_unconfigured"


class TestCallback:
    def _install_payload(self) -> dict[str, Any]:
        return {
            "id": 7,
            "account": {"login": "octo", "type": "Organization"},
            "repository_selection": "selected",
        }

    def test_round_trip(
        self, client: Any, auth: dict, admin_auth: dict, app_service: FakeClient
    ) -> None:
        app_service.installations = [self._install_payload()]
        app_service.repos[7] = ["octo/hello", "octo/world"]
        begin = client.post("/v1/github/app/authorize", headers=auth).json()
        resp = client.post(
            "/v1/github/app/authorize/callback",
            json={"installation_id": 7, "state": begin["state"]},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        inst = resp.json()["installation"]
        assert inst["installation_id"] == 7
        assert inst["repository_selection"] == "selected"
        assert inst["repositories"] == ["octo/hello", "octo/world"]
        assert MINTED not in resp.text  # the minted token never surfaces

        # state is single-use — replaying the callback fails 403
        replay = client.post(
            "/v1/github/app/authorize/callback",
            json={"installation_id": 7, "state": begin["state"]},
            headers=auth,
        )
        assert replay.status_code == 403
        assert replay.json()["error"]["code"] == "github_app_state"

        # posture now reports the installation
        status = client.get("/v1/github/app", headers=auth).json()
        assert [i["installation_id"] for i in status["installations"]] == [7]

    def test_malformed_body_is_400(self, client: Any, auth: dict, app_service: FakeClient) -> None:
        for body in (
            {},
            {"installation_id": "x", "state": "s"},
            {"installation_id": 7},  # missing state
        ):
            resp = client.post("/v1/github/app/authorize/callback", json=body, headers=auth)
            assert resp.status_code == 400, (body, resp.text)

    def test_wrong_state_is_403(self, client: Any, auth: dict, app_service: FakeClient) -> None:
        app_service.installations = [self._install_payload()]
        client.post("/v1/github/app/authorize", headers=auth)
        resp = client.post(
            "/v1/github/app/authorize/callback",
            json={"installation_id": 7, "state": "not-issued"},
            headers=auth,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "github_app_state"

    def test_unknown_installation_is_404(
        self, client: Any, auth: dict, app_service: FakeClient
    ) -> None:
        app_service.installations = [self._install_payload()]
        begin = client.post("/v1/github/app/authorize", headers=auth).json()
        resp = client.post(
            "/v1/github/app/authorize/callback",
            json={"installation_id": 99, "state": begin["state"]},
            headers=auth,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


class TestSyncAndRevoke:
    def _service(self, v1_env: Any) -> GitHubAppService:
        return v1_env.app.state.github_app

    def test_sync_requires_admin(
        self, client: Any, auth: dict, admin_auth: dict, app_service: FakeClient
    ) -> None:
        resp = client.post("/v1/github/app/sync", headers=auth)
        assert resp.status_code == 403
        app_service.installations = [
            {
                "id": 7,
                "account": {"login": "octo", "type": "Organization"},
                "repository_selection": "all",
            }
        ]
        resp = client.post("/v1/github/app/sync", headers=admin_auth)
        assert resp.status_code == 200
        assert [i["installation_id"] for i in resp.json()["installations"]] == [7]

    def test_revoke_requires_admin_and_reconnects(
        self, client: Any, auth: dict, admin_auth: dict, app_service: FakeClient
    ) -> None:
        service_repo = app_service
        app_service.installations = [
            {
                "id": 7,
                "account": {"login": "octo", "type": "Organization"},
                "repository_selection": "selected",
            }
        ]
        begin = client.post("/v1/github/app/authorize", headers=auth).json()
        client.post(
            "/v1/github/app/authorize/callback",
            json={"installation_id": 7, "state": begin["state"]},
            headers=auth,
        )
        # agents scope cannot revoke
        assert client.delete("/v1/github/app/installations/7", headers=auth).status_code == 403
        resp = client.delete("/v1/github/app/installations/7", headers=admin_auth)
        assert resp.status_code == 200
        assert resp.json() == {"revoked": 1, "remote_deleted": True}
        assert service_repo.deleted == [7]
        status = client.get("/v1/github/app", headers=auth).json()
        assert status["installations"] == []
        # reconnect: authorize again works with the same store/service
        begin2 = client.post("/v1/github/app/authorize", headers=auth).json()
        resp2 = client.post(
            "/v1/github/app/authorize/callback",
            json={"installation_id": 7, "state": begin2["state"]},
            headers=auth,
        )
        assert resp2.status_code == 200

    def test_revoke_unknown_is_404(
        self, client: Any, admin_auth: dict, app_service: FakeClient
    ) -> None:
        resp = client.delete("/v1/github/app/installations/404", headers=admin_auth)
        assert resp.status_code == 404
