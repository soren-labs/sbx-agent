"""WP2-H fixtures. Real Modal credentials stay in the environment."""

from __future__ import annotations

import os

import httpx
import pytest

from tests.e2e_modal.helpers import (
    artifacts_dir,
    client_for,
    close_active_sessions,
    discover_control_url,
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("SBX_E2E_MODAL") == "1":
        return
    skip = pytest.mark.skip(reason="set SBX_E2E_MODAL=1 to run real Modal+Codex e2e")
    for item in items:
        item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_cloud_env():
    """Override tests/conftest.py: this package needs Modal + Codex credentials."""
    yield


@pytest.fixture(scope="session")
def control_url() -> str:
    return discover_control_url()


@pytest.fixture(scope="session")
def artifacts() -> object:
    return artifacts_dir()


@pytest.fixture(scope="session")
def client(control_url: str) -> httpx.Client:
    http = client_for(control_url)
    close_active_sessions(http)
    yield http
    close_active_sessions(http)
    http.close()
