"""Shared fixtures for control-plane HTTP tests."""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest
from control.app import create_app
from control.backend import LocalProcessBackend
from control.store import InMemoryStore
from fastapi.testclient import TestClient

AUTH = ("sbx", "sbx")


@pytest.fixture
def control_env(stub_runner) -> Iterator[tuple[object, LocalProcessBackend, InMemoryStore]]:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    app = create_app(
        backend=backend,
        store=store,
        runner_cmd=[sys.executable, str(stub_runner)],
        basic_user="sbx",
        basic_password="sbx",
        keepalive_s=0.25,
    )
    try:
        yield app, backend, store
    finally:
        for handle in list(backend.list()):
            backend.terminate(handle)


@pytest.fixture
def client(control_env) -> Iterator[TestClient]:
    app, _, _ = control_env
    with TestClient(app) as test_client:
        yield test_client
