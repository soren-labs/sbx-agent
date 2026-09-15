"""Shared pytest fixtures. Strip cloud credentials so tests never depend on them."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_CLOUD_PREFIXES = (
    "MODAL_",
    "OPENAI_",
    "CODEX_",
    "XAI_",
    "ANTHROPIC_",
    "OPENCODE_",
    "GEMINI_",
    "GOOGLE_",
    "DEVIN_",
    "CLOUDFLARE_",
    "SBX_BASIC_",
    "SBX_ACCOUNT_",
    "SBX_PROVIDER_",
    "SBX_LINEAR_",
    "LINEAR_",
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
        "SBX_ACCOUNT_CREDENTIAL",
        "DEVIN_API_KEY",
        "XDG_RUNTIME_DIR",
    }
)


@pytest.fixture(autouse=True)
def _no_cloud_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Drop host credentials and isolate HOME/XDG (SOR-55/SOR-56)."""
    for key in list(os.environ):
        if key in _CLOUD_KEYS or key.startswith(_CLOUD_PREFIXES):
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


@pytest.fixture
def fake_codex(repo_root: Path) -> Path:
    return repo_root / "tests" / "fakes" / "fake_codex.py"


@pytest.fixture
def stub_runner(repo_root: Path) -> Path:
    return repo_root / "tests" / "fakes" / "stub_runner.py"
