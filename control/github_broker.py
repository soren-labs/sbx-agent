"""SBX-side client for the Sorenforge GitHub integration broker (SOR-220).

Default Connect GitHub path: when the deployment has *no* local GitHub App
(no ``SBX_GITHUB_APP_*`` env config and no manifest-registered App), the
control plane talks to the centrally hosted broker — a small trusted service
that holds the public SBX GitHub App's private key. This client only ever
sees:

- an install URL on ``github.com/apps/<slug>/installations/new`` (browser);
- installation metadata + a per-installation broker credential (server-side);
- short-lived installation access tokens it asked for (minted per call).

The App private key is NEVER requested, received, stored, or logged here —
that is the whole point of the broker boundary. The ``sbxbrk_`` credential
is deployment-scoped and revocable; it is stored hashed at the broker and
in the deployment's protected metadata lane (file store ``0600`` / Modal
Dict), never in API responses, agent workspaces, or evidence.

``SBX_GITHUB_BROKER_URL`` overrides the broker origin (self-hosted broker /
dev / tests); set it to ``off`` to disable the broker default entirely and
keep only the fully self-hosted paths (env App, manifest flow, PAT bridge).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from control.github_app import _parse_github_time

BROKER_URL_ENV = "SBX_GITHUB_BROKER_URL"
# The hosted default — a pre-registered public GitHub App behind it. An
# empty/"off" value disables the broker lane (fully self-hosted posture).
DEFAULT_BROKER_URL = "https://github-broker.sorenforge.com"
_BROKER_DISABLED = {"", "off", "disabled", "none"}


class GitHubBrokerError(Exception):
    """Broker call failures; the service layer maps these onto the
    ``github_broker_*`` canonical codes."""

    def __init__(self, code: str, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def broker_url_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """The broker origin in effect, or None when the lane is disabled."""
    env = os.environ if env is None else env
    raw = (env.get(BROKER_URL_ENV) or DEFAULT_BROKER_URL).strip().rstrip("/")
    return None if raw.lower() in _BROKER_DISABLED else raw


class GitHubBrokerClient:
    """Minimal httpx client for the broker API — injectable transport like
    ``GitHubAppClient`` so tests and the e2e seam stay cloud-free."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 15.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._client = httpx.Client(transport=transport, timeout=timeout_s)
        import time

        self._clock = clock or time.time

    @property
    def base_url(self) -> str:
        return self._base

    def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> Any:
        """One broker call. Error bodies are clipped (200 chars) — upstream
        may echo request fragments and must never leak into our messages."""
        try:
            resp = self._client.request(
                method, f"{self._base}{path}", json=json_body, timeout=self._timeout_s
            )
        except httpx.HTTPError as exc:
            raise GitHubBrokerError(
                "broker_unreachable", f"broker request failed: {type(exc).__name__}"
            ) from exc
        data: Any = None
        if resp.content:
            try:
                data = resp.json()
            except ValueError:
                data = None
        if resp.status_code >= 400:
            err = data.get("error") if isinstance(data, dict) else None
            code = str((err or {}).get("code") or "broker_error")
            message = str((err or {}).get("message") or f"broker returned {resp.status_code}")
            raise GitHubBrokerError(code, message[:200], status_code=resp.status_code)
        return data

    def create_session(self, redirect_uri: str) -> dict[str, Any]:
        """``POST /v1/github/install/sessions`` → signed install session:
        ``{install_url, state, expires_at}`` where ``install_url`` is the
        official ``github.com/apps/<slug>/installations/new`` page."""
        data = self._request(
            "POST", "/v1/github/install/sessions", json_body={"redirect_uri": redirect_uri}
        )
        if not isinstance(data, dict) or not str(data.get("install_url") or ""):
            raise GitHubBrokerError("broker_error", "broker returned no install_url")
        return data

    def claim(self, code: str) -> dict[str, Any]:
        """``POST /v1/github/installations/claim`` — one-time exchange of the
        redirect's claim code → ``{installation, credential}``."""
        data = self._request("POST", "/v1/github/installations/claim", json_body={"code": code})
        if not isinstance(data, dict) or not data.get("credential"):
            raise GitHubBrokerError("broker_error", "broker claim returned no credential")
        return data

    def mint_token(
        self, installation_id: int, credential: str, *, repositories: list[str] | None = None
    ) -> tuple[str, float]:
        """``POST /v1/github/installations/{id}/tokens`` → (token, expiry)."""
        body: dict[str, Any] = {"credential": credential}
        if repositories:
            body["repositories"] = repositories
        data = self._request(
            "POST", f"/v1/github/installations/{installation_id}/tokens", json_body=body
        )
        token = str((data or {}).get("token") or "")
        if not token:
            raise GitHubBrokerError("broker_error", "broker returned no token")
        try:
            expiry = _parse_github_time((data or {}).get("expires_at"))
        except ValueError:
            expiry = self._clock() + 3600
        return token, expiry

    def sync_installation(self, installation_id: int, credential: str) -> dict[str, Any]:
        """``POST /v1/github/installations/{id}/sync`` → refreshed metadata;
        404 means the installation is gone (fail closed upstream too)."""
        data = self._request(
            "POST",
            f"/v1/github/installations/{installation_id}/sync",
            json_body={"credential": credential},
        )
        return data if isinstance(data, dict) else {}

    def revoke_installation(self, installation_id: int, credential: str) -> dict[str, Any]:
        """``DELETE /v1/github/installations/{id}`` → ``{revoked, remote_deleted}``."""
        # httpx supports a JSON body on DELETE via ``request``.
        data = self._request(
            "DELETE",
            f"/v1/github/installations/{installation_id}",
            json_body={"credential": credential},
        )
        return data if isinstance(data, dict) else {"revoked": 1}

    def health(self) -> dict[str, Any]:
        data = self._request("GET", "/healthz")
        return data if isinstance(data, dict) else {"ok": False}


def client_from_env(
    env: Mapping[str, str] | None = None, *, transport: httpx.BaseTransport | None = None
) -> GitHubBrokerClient | None:
    """The env-configured broker client, or None when the lane is off."""
    url = broker_url_from_env(env)
    if url is None:
        return None
    return GitHubBrokerClient(url, transport=transport)


__all__ = [
    "BROKER_URL_ENV",
    "DEFAULT_BROKER_URL",
    "GitHubBrokerClient",
    "GitHubBrokerError",
    "broker_url_from_env",
    "client_from_env",
]
