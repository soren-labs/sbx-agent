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
        "SBX_ACCOUNT_CREDENTIAL",
        "DEVIN_API_KEY",
        "XDG_RUNTIME_DIR",
        # Provider-specific auth bridges.
        "ACP_BACKEND",
        "WINDSURF_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        # Control-plane secrets and credential-forwarding triggers.
        "SBX_V1_BOOTSTRAP_KEY",
        "SBX_GITHUB_EPHEMERAL",
        "SBX_LINEAR_MCP_EPHEMERAL",
        "SBX_API_USER",
        "SBX_API_PASSWORD",
        # An ambient work dir must never redirect sandbox writes off tmp_path.
        "SBX_WORK",
        # Ambient backend/transport selectors are per-test inputs, not host
        # state — ``SBX_BACKEND=modal`` in a shell would otherwise import
        # ``modal`` at collection time.
        "SBX_DEVIN_TRANSPORT",
        "SBX_BACKEND",
        # Ambient store-dir overrides would redirect the import-time app's
        # local stores off the XDG-isolated home.
        "SBX_RUN_STORE_DIR",
        "SBX_ARTIFACT_STORE_DIR",
        "SBX_WORKSPACE_STORE_DIR",
        "SBX_WORKFLOW_STORE_DIR",
    }
)
# Deliberately NOT scrubbed: SBX_V1_API_KEY / SBX_V1_BASE_URL /
# SBX_POOL_GATE_REAL are the opt-in inputs of the real acceptance gate
# (tests/acceptance, outside testpaths); tests/e2e_modal overrides this
# fixture when SBX_E2E_MODAL=1.


def _is_cloud_key(key: str) -> bool:
    return key in _CLOUD_KEYS or key.startswith(_CLOUD_PREFIXES)


# Collection-time scrub (SOR-55/SOR-101): ``control.app`` builds a FastAPI app
# at import, and ambient host env (``SBX_V1_BOOTSTRAP_KEY``,
# ``SBX_BACKEND=modal``, provider credentials) would otherwise leak into —
# or break — collection before any fixture runs. The autouse fixture below
# re-applies the scrub per-test so late monkeypatch snapshots stay clean.
# ``SBX_E2E_MODAL=1`` is the documented opt-out: the Modal e2e suite needs
# the real credentials it is handed.
if os.environ.get("SBX_E2E_MODAL") != "1":
    for _key in list(os.environ):
        if _is_cloud_key(_key):
            os.environ.pop(_key, None)


@pytest.fixture(autouse=True)
def _no_cloud_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Drop host credentials and isolate HOME/XDG (SOR-55/SOR-56)."""
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


@pytest.fixture
def fake_codex(repo_root: Path) -> Path:
    return repo_root / "tests" / "fakes" / "fake_codex.py"


@pytest.fixture
def stub_runner(repo_root: Path) -> Path:
    return repo_root / "tests" / "fakes" / "stub_runner.py"
