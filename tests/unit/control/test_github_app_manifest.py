"""SOR-220: GitHub App Manifest zero-config registration — service tests.

Deterministic and cloud-free: the GitHub API is faked with a duck-typed
client or ``httpx.MockTransport``; generated PEM material stays in-memory
and is asserted absent from every API-surface payload.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from control.github_app import (
    DEFAULT_API_URL,
    GitHubAppClient,
    GitHubAppConfig,
    GitHubAppError,
    GitHubAppService,
    InMemoryGitHubAppStore,
    github_web_url,
)


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


class ManifestClient:
    """Duck-typed client faking the manifest conversion + install APIs."""

    def __init__(self, pem: str = "PEM-PLACEHOLDER") -> None:
        self.pem = pem
        self.conversions: list[str] = []
        self.installations: list[dict[str, Any]] = []
        self.repos: dict[int, list[str]] = {}

    def exchange_manifest_code(self, code: str) -> dict[str, Any]:
        self.conversions.append(code)
        return {
            "id": 99001,
            "slug": "sbx-registered",
            "client_id": "Iv1.fakeclient",
            "client_secret": "fake-client-secret",
            "pem": self.pem,
            "webhook_secret": "fake-webhook-secret",
            "name": "sbx-registered",
            "html_url": "https://github.com/apps/sbx-registered",
        }

    def list_installations(self) -> list[dict[str, Any]]:
        return list(self.installations)

    def create_installation_token(
        self, installation_id: int, *, repositories: list[str] | None = None
    ) -> tuple[str, float]:
        return "ghs_fake_installation_token", time.time() + 3600

    def installation_repositories(self, token: str) -> tuple[str, list[str]]:
        return "selected", self.repos.get(7, ["octo/hello"])

    def delete_installation(self, installation_id: int) -> bool:
        return True


class RecordingSecretWriter:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.fail = fail

    def refresh(self, name: str, env: dict[str, str]) -> None:
        if self.fail:
            raise RuntimeError("secret backend down")
        self.calls.append((name, env))


def make_service(
    *,
    env_config: GitHubAppConfig | None = None,
    secret_writer: Any = None,
    secret_name: str | None = "sbx-github-app",
) -> tuple[GitHubAppService, ManifestClient, InMemoryGitHubAppStore]:
    client = ManifestClient()
    store = InMemoryGitHubAppStore()
    service = GitHubAppService(
        env_config or GitHubAppConfig(),
        store,
        client,
        api_url=DEFAULT_API_URL,
        secret_writer=secret_writer,
        secret_name=secret_name,
    )
    return service, client, store


def test_github_web_url_ghe() -> None:
    assert github_web_url(DEFAULT_API_URL) == "https://github.com"
    assert github_web_url("https://ghe.acme.io/api/v3") == "https://ghe.acme.io"


def test_begin_manifest_shape() -> None:
    service, _, store = make_service()
    out = service.begin_manifest(
        redirect_url="https://sbx.example.io/v1/github/app/manifest/callback"
    )
    assert out["manifest_url"].startswith("https://github.com/settings/apps/new?state=")
    manifest = out["manifest"]
    assert manifest["redirect_url"] == "https://sbx.example.io/v1/github/app/manifest/callback"
    assert manifest["hook_attributes"]["active"] is False
    assert manifest["hook_attributes"]["url"].endswith("/v1/github/app/webhook")
    assert manifest["public"] is False
    assert manifest["default_permissions"]["contents"] == "write"
    # The pending state is the single-use capability binding the two steps.
    assert store.pop_state(f"manifest:{out['state']}") is not None


def test_begin_manifest_org_url() -> None:
    service, _, _ = make_service()
    out = service.begin_manifest(redirect_url="https://sbx.example.io/cb", org="acme-org")
    assert out["manifest_url"].startswith(
        "https://github.com/organizations/acme-org/settings/apps/new"
    )
    service2, _, _ = make_service()
    out2 = service2.begin_manifest(redirect_url="https://sbx.example.io/cb", name="my-app")
    assert out2["manifest"]["name"] == "my-app"


def test_begin_manifest_rejects_when_configured(app_private_key: str) -> None:
    service, _, _ = make_service(
        env_config=GitHubAppConfig(app_id="1", slug="x", private_key=app_private_key)
    )
    with pytest.raises(GitHubAppError) as exc:
        service.begin_manifest(redirect_url="https://sbx.example.io/cb")
    assert exc.value.code == "github_app_configured"
    assert exc.value.status_code == 409


def test_begin_manifest_rejects_bad_redirect() -> None:
    service, _, _ = make_service()
    with pytest.raises(GitHubAppError) as exc:
        service.begin_manifest(redirect_url="not-a-url")
    assert exc.value.code == "github_app_invalid"


def test_complete_manifest_registers_and_is_usable(
    app_private_key: str,
) -> None:
    """Zero-config: after the conversion the App is usable with no
    redeploy — the next authorize step resolves the registry config."""
    writer = RecordingSecretWriter()
    service, client, store = make_service(secret_writer=writer)
    begun = service.begin_manifest(redirect_url="https://sbx.example.io/cb")
    client.pem = app_private_key

    out = service.complete_manifest("conversion-code", begun["state"])
    assert client.conversions == ["conversion-code"]
    assert out["source"] == "registry"
    assert out["slug"] == "sbx-registered"
    assert out["secret"] == "refreshed"
    # Metadata only — private material never surfaces.
    import json as _json

    blob = _json.dumps(out)
    for needle in (app_private_key, "fake-client-secret", "fake-webhook-secret", "pem"):
        assert needle not in blob
    # Secret writer saw the deployment-managed material (SBX_* env keys).
    assert writer.calls and writer.calls[0][0] == "sbx-github-app"
    assert "SBX_GITHUB_APP_PRIVATE_KEY" in writer.calls[0][1]

    # Status now reports configured from the registry lane.
    status = service.status()
    assert status["configured"] is True
    assert status["source"] == "registry"
    assert status["app_url"] == "https://github.com/apps/sbx-registered"

    # And the one-click authorize flow works without a redeploy.
    auth = service.begin_authorization()
    assert auth["authorize_url"].startswith(
        "https://github.com/apps/sbx-registered/installations/new?state="
    )


def test_complete_manifest_state_single_use(app_private_key: str) -> None:
    service, _, _ = make_service()
    begun = service.begin_manifest(redirect_url="https://sbx.example.io/cb")
    service.complete_manifest("code-1", begun["state"])
    # State consumed: replaying it is a 403, not a second registration.
    with pytest.raises(GitHubAppError) as exc:
        service.complete_manifest("code-2", begun["state"])
    assert exc.value.code == "github_app_state"
    assert exc.value.status_code == 403


def test_complete_manifest_unknown_state() -> None:
    service, _, _ = make_service()
    with pytest.raises(GitHubAppError) as exc:
        service.complete_manifest("code", "bogus")
    assert exc.value.code == "github_app_state"


def test_complete_manifest_requires_code() -> None:
    service, _, _ = make_service()
    with pytest.raises(GitHubAppError) as exc:
        service.complete_manifest("", "x")
    assert exc.value.code == "github_app_invalid"


def test_complete_manifest_expired_state() -> None:
    now = [1_000.0]
    client = ManifestClient()
    service = GitHubAppService(
        GitHubAppConfig(),
        InMemoryGitHubAppStore(),
        client,
        clock=lambda: now[0],
    )
    begun = service.begin_manifest(redirect_url="https://sbx.example.io/cb")
    now[0] += 900
    with pytest.raises(GitHubAppError) as exc:
        service.complete_manifest("code", begun["state"])
    assert exc.value.code == "github_app_state"


def test_secret_writer_failure_is_nonfatal(app_private_key: str) -> None:
    writer = RecordingSecretWriter(fail=True)
    service, client, _ = make_service(secret_writer=writer)
    begun = service.begin_manifest(redirect_url="https://sbx.example.io/cb")
    client.pem = app_private_key
    out = service.complete_manifest("code", begun["state"])
    assert out["secret"] == "failed"
    # Registry lane still became authoritative.
    assert service.status()["configured"] is True


def test_no_secret_writer_reports_skipped(app_private_key: str) -> None:
    service, client, _ = make_service()
    begun = service.begin_manifest(redirect_url="https://sbx.example.io/cb")
    client.pem = app_private_key
    assert service.complete_manifest("code", begun["state"])["secret"] == "skipped"


def test_env_config_wins_over_registry(app_private_key: str) -> None:
    """SOR-177 backcompat: env-configured deployment ignores the registry
    record entirely."""
    service, client, _ = make_service(
        env_config=GitHubAppConfig(app_id="42", slug="env-app", private_key=app_private_key)
    )
    # Manually plant a registry record — resolution must still prefer env.
    service._store.put_app_config(
        {
            "app_id": "99",
            "slug": "reg-app",
            "private_key": app_private_key,
            "client_id": "",
            "client_secret": "",
            "webhook_secret": "",
            "name": "reg",
            "html_url": "",
        }
    )
    status = service.status()
    assert status["source"] == "env"
    assert status["app_slug"] == "env-app"


def test_exchange_manifest_code_is_unauthenticated() -> None:
    """``POST /app-manifests/{code}/conversions`` must not carry the app's
    JWT — GitHub requires the conversion call unauthenticated."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            201,
            json={
                "id": 1,
                "slug": "sbx",
                "client_id": "Iv1.x",
                "client_secret": "s",
                "pem": "p",
                "webhook_secret": "w",
                "name": "sbx",
                "html_url": "https://github.com/apps/sbx",
            },
        )

    transport = httpx.MockTransport(handler)
    client = GitHubAppClient(
        GitHubAppConfig(app_id="1", slug="sbx", private_key="PEM-PLACEHOLDER"),
        api_url=DEFAULT_API_URL,
        transport=transport,
    )
    data = client.exchange_manifest_code("abc123")
    assert data["id"] == 1
    assert seen["authorization"] is None
