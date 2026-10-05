"""Shared pytest fixtures. Strip cloud credentials so tests never depend on them."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest_plugins = ["tests.support.postgres"]

_CLOUD_PREFIXES = (
    "MODAL_",
    "OPENAI_",
    "CODEX_",
    "OPENCODE_",
    "ANTHROPIC_",
    "GEMINI_",
    "GOOGLE_",
    "AWS_",
    "AZURE_",
    "CLOUDFLARE_",
    # Opt-in live/benchmark inputs (Modal, GitHub, OpenCode Zen, account password).
    "SBX_TEST_",
    "SBX_BENCHMARK_",
)
_CLOUD_KEYS = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "DATABASE_URL",
        "XDG_RUNTIME_DIR",
        # Control-plane configuration must be passed explicitly by each test.
        "SBX_DATABASE_URL",
        "SBX_VAULT_KEYS",
        "SBX_RUNTIME_MASTER_KEY",
        "SBX_RESEND_API_KEY",
        "SBX_EXECUTORS",
        "SBX_PUBLIC_URL",
        "SBX_DATA_DIR",
        # Ambient CLI pointers must never steer tests at a real deployment.
        "SBX_BASE_URL",
        "SBX_API_KEY",
    }
)


def _is_cloud_key(key: str) -> bool:
    return key in _CLOUD_KEYS or key.startswith(_CLOUD_PREFIXES)


# Collection-time scrub, re-applied per test by the autouse fixture below.
for _key in list(os.environ):
    if _is_cloud_key(_key):
        os.environ.pop(_key, None)


@pytest.fixture(autouse=True)
def _no_cloud_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Drop host credentials and isolate HOME/XDG."""
    for key in list(os.environ):
        if _is_cloud_key(key):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent
