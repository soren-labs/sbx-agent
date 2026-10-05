"""Shared pytest fixtures. Strip cloud credentials so tests never depend on them."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_CLOUD_PREFIXES = (
    # Provider + platform credentials/config.
    "MODAL_",
    "OPENAI_",
    "CODEX_",
    "XAI_",
    "ANTHROPIC_",
    "OPENCODE_",
    "GEMINI_",
    "GOOGLE_",
    "DEVIN_",
    "GROK_",
    "AGY_",
    "CLOUDFLARE_",
    "AWS_",
    "AZURE_",
    "HF_",
    # Control-plane credential bridges.
    "SBX_BASIC_",
    "SBX_ACCOUNT_",
    "SBX_PROVIDER_",
    "SBX_LINEAR_",
    "LINEAR_",
    # Ambient version-resolution knobs (locks, registries, overrides) are
    # per-test inputs — ambient values must never pick versions in tests.
    "SBX_VERSIONS_",
    # Fake-runner knobs are set per-test; ambient values must never leak in.
    "FAKE_",
)
_CLOUD_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_AUTH_JSON",
        "CODEX_HOME",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "MODAL_TOKEN",
        "DEVIN_API_KEY",
        "XDG_RUNTIME_DIR",
        # Provider-specific auth bridges.
        "ACP_BACKEND",
        "WINDSURF_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        # Control-plane secrets.
        "DATABASE_URL",
        "SBX_GITHUB_APP_ID",
        "SBX_GITHUB_APP_SLUG",
        "SBX_GITHUB_APP_PRIVATE_KEY",
        "SBX_API_USER",
        "SBX_API_PASSWORD",
        # Ambient CLI/deployment pointers on a dev host must never steer
        # tests at a real deployment or leak a real API key.
        "SBX_BASE_URL",
    }
)


def _is_cloud_key(key: str) -> bool:
    return key in _CLOUD_KEYS or any(key.startswith(p) for p in _CLOUD_PREFIXES)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    monkeypatch.setenv("SBX_BACKEND", "local")


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent
