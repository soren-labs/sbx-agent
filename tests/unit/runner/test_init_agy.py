"""SOR-258 regression: fresh-HOME proof for the AntiGravity auth bundle.

A blob carrying only ``antigravity-oauth-token`` restored by ``runner
init`` into a fresh ``$SBX_WORK/home`` must run the canonical ``agy
models`` auth/model probe — the pre-flight bug was the real CLI
answering "account not eligible" on exactly that layout. The fake CLI
reproduces the gate via ``FAKE_AGY_REQUIRE_ONBOARDING=1``: without the
onboarding marker it refuses `models` the way the real 1.2.x CLI does.

No cloud credentials, no real CLI — ``AGY_BIN`` stays pointed at
``tests/fakes/fake_agy.py`` like the other runner lanes.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from runtime.runner.adapters.antigravity import (
    OAUTH_TOKEN_REL,
    ONBOARDING_STATE,
    ONBOARDING_STATE_REL,
)
from tests.unit.runner.conftest import load_json, run_runner

MODEL = "gemini-3.8-flash-low"
FAKE_AGY = Path(__file__).resolve().parents[3] / "tests" / "fakes" / "fake_agy.py"
TOKEN_JSON = '{"auth_method":"consumer","id_token":"REDACTED","token":"REDACTED"}\n'


@pytest.fixture
def agy_env(work: Path, repo_root: Path) -> dict[str, str]:
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "SBX_WORK": str(work),
        "PYTHONPATH": str(repo_root),
        "PYTHONUNBUFFERED": "1",
        "AGY_BIN": str(FAKE_AGY),
        "SBX_BACKEND": "local",
    }


def _agy_models(home: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the canonical auth probe against ``home`` via the fake CLI."""
    return subprocess.run(
        [sys.executable, str(FAKE_AGY), "models"],
        env={**env, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=30.0,
        check=False,
    )


def test_fake_gate_rejects_token_only_home(work: Path, agy_env: dict[str, str]) -> None:
    """Control: without the marker the probe fails the way the real CLI does —
    the test can't pass vacuously if the gate is never exercised."""
    agy_env["FAKE_AGY_REQUIRE_ONBOARDING"] = "1"
    home = work / "home"
    token = home / OAUTH_TOKEN_REL
    token.parent.mkdir(parents=True)
    token.write_text(TOKEN_JSON, encoding="utf-8")

    probe = _agy_models(home, agy_env)
    assert probe.returncode == 1
    assert "not eligible" in (probe.stderr + probe.stdout)


def test_init_repairs_token_only_blob_for_probe(work: Path, agy_env: dict[str, str]) -> None:
    """The bug scenario: SBX restores a token-only blob into a fresh HOME —
    init must reconstruct the companion so `agy models` passes."""
    agy_env["FAKE_AGY_REQUIRE_ONBOARDING"] = "1"
    agy_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {"provider": "antigravity", "files": {OAUTH_TOKEN_REL: TOKEN_JSON}}
    )

    result = run_runner(["init", "--provider", "antigravity", "--model", MODEL], agy_env)
    assert result.returncode == 0, result.stderr

    home = work / "home"
    marker = home / ONBOARDING_STATE_REL
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8") == ONBOARDING_STATE
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert stat.S_IMODE(marker.parent.stat().st_mode) == 0o700

    session = load_json(work / "session.json")
    assert session["credential_files"] == sorted([OAUTH_TOKEN_REL, ONBOARDING_STATE_REL])

    probe = _agy_models(home, agy_env)
    assert probe.returncode == 0, probe.stderr
    assert "Logged in" in probe.stdout
    assert "gemini-3.8-flash-low" in probe.stdout


def test_init_restored_marker_probe_passes(work: Path, agy_env: dict[str, str]) -> None:
    """Full bundle restore: blob-supplied marker is kept verbatim and the
    canonical probe passes on it."""
    agy_env["FAKE_AGY_REQUIRE_ONBOARDING"] = "1"
    marker_json = '{"consumerOnboardingComplete":true,"onboardingComplete":true,"custom":2}\n'
    agy_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {
            "provider": "antigravity",
            "files": {OAUTH_TOKEN_REL: TOKEN_JSON, ONBOARDING_STATE_REL: marker_json},
        }
    )

    result = run_runner(["init", "--provider", "antigravity", "--model", MODEL], agy_env)
    assert result.returncode == 0, result.stderr

    home = work / "home"
    assert (home / ONBOARDING_STATE_REL).read_text(encoding="utf-8") == marker_json
    probe = _agy_models(home, agy_env)
    assert probe.returncode == 0, probe.stderr
    assert "gemini-3.8-flash-low" in probe.stdout


def test_export_writes_back_full_bundle(work: Path, agy_env: dict[str, str]) -> None:
    """Migration for legacy token-only records: export carries the
    reconstructed marker so a write-back commits the complete bundle."""
    agy_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {"provider": "antigravity", "files": {OAUTH_TOKEN_REL: TOKEN_JSON}}
    )
    result = run_runner(["init", "--provider", "antigravity", "--model", MODEL], agy_env)
    assert result.returncode == 0, result.stderr

    out = run_runner(["export-credentials"], agy_env)
    assert out.returncode == 0
    exported = json.loads(out.stdout.strip())
    assert exported["files"] == {
        OAUTH_TOKEN_REL: TOKEN_JSON,
        ONBOARDING_STATE_REL: ONBOARDING_STATE,
    }
