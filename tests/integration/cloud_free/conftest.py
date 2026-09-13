"""WP2-F (SOR-40): real runtime.runner + LocalProcessBackend + fake_codex."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from control.app import create_app
from control.backend import LocalProcessBackend
from control.store import InMemoryStore
from fastapi.testclient import TestClient

AUTH = ("sbx", "sbx")
RUNNER_CMD = [sys.executable, "-m", "runtime.runner"]


@pytest.fixture
def live_env(
    monkeypatch: pytest.MonkeyPatch, fake_codex: Path
) -> Iterator[tuple[object, LocalProcessBackend, InMemoryStore]]:
    """Control plane wired to the real runner; Codex is fake_codex (no cloud)."""
    monkeypatch.setenv("SBX_BACKEND", "local")
    monkeypatch.setenv("CODEX_BIN", str(fake_codex))
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
    monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "2")
    monkeypatch.setenv("PYTHONUNBUFFERED", "1")
    backend = LocalProcessBackend()
    store = InMemoryStore()
    app = create_app(
        backend=backend,
        store=store,
        runner_cmd=RUNNER_CMD,
        basic_user="sbx",
        basic_password="sbx",
        keepalive_s=0.4,
    )
    # Safety net so a leaked hang scenario cannot hold pytest for 900s.
    app.state.plane.turn_max_seconds = 30
    try:
        yield app, backend, store
    finally:
        for handle in list(backend.list()):
            backend.terminate(handle)


@pytest.fixture
def client(live_env) -> Iterator[TestClient]:
    app, _, _ = live_env
    with TestClient(app) as test_client:
        yield test_client
