"""Shared pytest fixtures. Strip cloud credentials so tests never depend on them."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_CLOUD_PREFIXES = ("MODAL_", "OPENAI_", "CODEX_API")
_CLOUD_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "MODAL_TOKEN",
    }
)


@pytest.fixture(autouse=True)
def _no_cloud_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key in _CLOUD_KEYS or key.startswith(_CLOUD_PREFIXES):
            monkeypatch.delenv(key, raising=False)
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
