"""Fixtures for ``/v1`` API tests: real ControlPlane + LocalProcessBackend + stub_runner.

The WP0 in-memory fakes are injected via ``app.state`` — the same seams P2-C
will use for the persistent implementations.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import uvicorn
from control.app import create_app
from control.backend import LocalProcessBackend
from control.ports import Account
from control.store import InMemoryStore
from fastapi.testclient import TestClient
from tests.fakes.fake_ports import (
    InMemoryAccountRegistry,
    InMemoryApiKeyStore,
    InMemoryScheduler,
)


def _iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class V1Env:
    app: Any
    backend: LocalProcessBackend
    store: InMemoryStore
    registry: InMemoryAccountRegistry
    scheduler: InMemoryScheduler
    keys: InMemoryApiKeyStore
    agents_token: str
    admin_token: str
    agents_key_id: str
    admin_key_id: str


@pytest.fixture
def v1_env(stub_runner, monkeypatch) -> Iterator[V1Env]:
    # SOR-210: no SBX_PROVIDERS means a platform-only deployment — the v1
    # test app exercises every provider's routes, so enable them all.
    monkeypatch.setenv("SBX_PROVIDERS", "codex,antigravity,grok,opencode,devin")
    backend = LocalProcessBackend()
    store = InMemoryStore()
    app = create_app(
        backend=backend,
        store=store,
        runner_cmd=[sys.executable, str(stub_runner)],
        keepalive_s=0.2,
    )
    registry = InMemoryAccountRegistry()
    scheduler = InMemoryScheduler(registry)
    keys = InMemoryApiKeyStore()
    app.state.account_registry = registry
    app.state.scheduler = scheduler
    app.state.api_key_store = keys
    registry.put(
        Account(
            id="acct-codex-1",
            provider="codex",
            label="codex account",
            models=("gpt-5.6-luna", "gpt-5.3-codex"),
            created_at=_iso(),
        )
    )
    agents_key, agents_token = keys.create(label="agents-key", scopes=("agents",))
    admin_key, admin_token = keys.create(label="admin-key", scopes=("admin", "agents"))
    env = V1Env(
        app=app,
        backend=backend,
        store=store,
        registry=registry,
        scheduler=scheduler,
        keys=keys,
        agents_token=agents_token,
        admin_token=admin_token,
        agents_key_id=agents_key.id,
        admin_key_id=admin_key.id,
    )
    try:
        yield env
    finally:
        for handle in list(backend.list()):
            backend.terminate(handle)


@pytest.fixture
def client(v1_env: V1Env) -> Iterator[TestClient]:
    with TestClient(v1_env.app) as test_client:
        yield test_client


@pytest.fixture
def auth(v1_env: V1Env) -> dict[str, str]:
    return {"Authorization": f"Bearer {v1_env.agents_token}"}


@pytest.fixture
def admin_auth(v1_env: V1Env) -> dict[str, str]:
    return {"Authorization": f"Bearer {v1_env.admin_token}"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def live_base(v1_env: V1Env) -> Iterator[str]:
    """Real uvicorn server for SSE tests (TestClient buffers streams)."""
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(v1_env.app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base}/v1/me", timeout=0.2)
            break
        except httpx.TransportError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        raise AssertionError("v1 app did not start")
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=2)


def seed_account(
    env: V1Env,
    account_id: str,
    provider: str = "codex",
    *,
    status: str = "active",
    max_concurrent: int = 1,
    models: tuple[str, ...] = (),
    secret_name: str = "",
) -> Account:
    account = Account(
        id=account_id,
        provider=provider,
        label=account_id,
        status=status,
        max_concurrent=max_concurrent,
        secret_name=secret_name,
        models=models,
        created_at=_iso(),
    )
    env.registry.put(account)
    return account


def create_agent(client: TestClient, auth: dict[str, str], **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "prompt": {"text": "Create hello.txt in the workspace."},
        "agent": {"provider": "codex"},
    }
    body.update(overrides)
    resp = client.post("/v1/agents", json=body, headers=auth)
    assert resp.status_code == 201, resp.text
    return resp.json()


def wait_run(
    client: TestClient,
    auth: dict[str, str],
    agent_id: str,
    run_id: str,
    *,
    timeout: float = 15.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/v1/agents/{agent_id}/runs/{run_id}", headers=auth).json()
        if run["status"] in ("FINISHED", "ERROR", "CANCELLED", "EXPIRED"):
            return run
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not reach a terminal state")


def wait_sandbox(v1_env: V1Env, agent_id: str, *, timeout: float = 15.0) -> Any:
    """Block until the async create's provisioner finishes (SOR-82 A2).

    Returns the record once it leaves ``creating`` — ``idle``/``running``
    means ``runner init`` completed; a terminal status means provision failed
    or the session was closed mid-flight.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = v1_env.store.get(agent_id)
        if rec is not None and rec.status != "creating":
            return rec
        time.sleep(0.05)
    raise AssertionError(f"agent {agent_id} was never provisioned")
