"""HTTP surface for the Sorenforge GitHub integration broker (SOR-220).

Deliberately the minimum API a self-hosted SBX deployment needs:

- ``POST /v1/github/install/sessions`` — start a signed install session
  (unauthenticated: the response is only the public GitHub install URL plus
  the signed state; the security lives in the signature, TTL, and the
  deployment ``redirect_uri`` baked into it).
- ``GET /v1/github/install/callback`` — the public App's Setup URL: GitHub
  sends the browser here post-install; we verify + bind, then 302 back to
  the originating deployment with a one-time claim ``code``.
- ``POST /v1/github/installations/claim`` — the deployment exchanges the
  one-time code for installation metadata + its broker credential.
- ``POST /v1/github/installations/{id}/tokens`` — mint a short-lived,
  least-privilege installation access token (credential required).
- ``POST /v1/github/installations/{id}/sync`` — refresh repo coverage.
- ``DELETE /v1/github/installations/{id}`` — revoke (best-effort uninstall
  upstream, always forgets the binding).
- ``GET /healthz`` — posture only.

No endpoint ever returns the App private key. Run with
``uvicorn broker.app:create_app --factory`` (or ``python -m broker``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from control.github_app import DEFAULT_API_URL
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from broker.service import (
    BROKER_API_URL_ENV,
    BROKER_STORE_DICT_ENV,
    BROKER_STORE_DIR_ENV,
    BrokerConfig,
    BrokerError,
    BrokerStore,
    FileBrokerStore,
    GitHubBrokerService,
    InMemoryBrokerStore,
)


class InstallSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    redirect_uri: str = Field(min_length=1)


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1)


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential: str = Field(min_length=1)
    repositories: list[str] | None = None


class CredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential: str = Field(min_length=1)


def _error(exc: BrokerError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status_code
    )


def default_service(env: dict[str, str] | None = None) -> GitHubBrokerService:
    """Env-configured broker. Store precedence: ``SBX_BROKER_STORE_DICT``
    (durable ``modal.Dict``) → ``SBX_BROKER_STORE_DIR`` (files) → in-memory
    (stateless deploys tolerate lost in-flight installs — the operator just
    clicks Connect again)."""
    env = os.environ if env is None else env
    store: BrokerStore
    if env.get(BROKER_STORE_DICT_ENV):
        from broker.service import ModalDictBrokerStore

        store = ModalDictBrokerStore(env[BROKER_STORE_DICT_ENV])
    elif env.get(BROKER_STORE_DIR_ENV):
        store = FileBrokerStore(env[BROKER_STORE_DIR_ENV])
    else:
        store = InMemoryBrokerStore()
    return GitHubBrokerService(
        BrokerConfig.from_env(env),
        store,
        api_url=env.get(BROKER_API_URL_ENV) or DEFAULT_API_URL,
    )


def create_app(service: GitHubBrokerService | None = None) -> FastAPI:
    app = FastAPI(title="sbx github broker", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.broker = service if service is not None else default_service()

    def broker(request: Request) -> GitHubBrokerService:
        return request.app.state.broker

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, Any]:
        return broker(request).health()

    @app.post("/v1/github/install/sessions", status_code=201)
    def begin(request: Request, body: InstallSessionRequest) -> dict[str, Any]:
        try:
            return broker(request).begin_install(body.redirect_uri)
        except BrokerError as exc:
            return _error(exc)

    @app.get("/v1/github/install/callback", response_model=None)
    def callback(
        request: Request, installation_id: int = 0, state: str = ""
    ) -> RedirectResponse | JSONResponse:
        try:
            redirect_uri, code = broker(request).handle_install_callback(installation_id, state)
        except BrokerError as exc:
            # Fail closed with no redirect: a forged state names no trusted
            # deployment, so there is nowhere safe to send the browser.
            return _error(exc)
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{sep}code={code}", status_code=302)

    @app.post("/v1/github/installations/claim", response_model=None)
    def claim(request: Request, body: ClaimRequest) -> dict[str, Any] | JSONResponse:
        try:
            return broker(request).claim(body.code)
        except BrokerError as exc:
            return _error(exc)

    @app.post("/v1/github/installations/{installation_id}/tokens", response_model=None)
    def mint(
        request: Request, installation_id: int, body: TokenRequest
    ) -> dict[str, Any] | JSONResponse:
        try:
            return broker(request).mint_token(
                installation_id, body.credential, repositories=body.repositories
            )
        except BrokerError as exc:
            return _error(exc)

    @app.post("/v1/github/installations/{installation_id}/sync", response_model=None)
    def sync(
        request: Request, installation_id: int, body: CredentialRequest
    ) -> dict[str, Any] | JSONResponse:
        try:
            return broker(request).sync_installation(installation_id, body.credential)
        except BrokerError as exc:
            return _error(exc)

    @app.delete("/v1/github/installations/{installation_id}", response_model=None)
    def revoke(
        request: Request, installation_id: int, body: CredentialRequest
    ) -> dict[str, Any] | JSONResponse:
        try:
            return broker(request).revoke_installation(installation_id, body.credential)
        except BrokerError as exc:
            return _error(exc)

    return app


def main() -> None:
    """``python -m broker`` — env config only; the private key stays in the
    process environment, never in argv or a file the repo controls."""
    import uvicorn

    store = os.environ.get(BROKER_STORE_DIR_ENV) or str(
        Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        / "sbx-github-broker"
    )
    os.environ.setdefault(BROKER_STORE_DIR_ENV, store)
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8780")))
