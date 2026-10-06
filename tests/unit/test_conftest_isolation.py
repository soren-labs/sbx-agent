"""tests/conftest.py strips cloud credentials but keeps explicit safe test DB config."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.conftest import _is_cloud_key

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "key",
    [
        "SBX_TEST_MODAL_TOKEN_ID",
        "SBX_TEST_GITHUB_TOKEN",
        "SBX_BENCHMARK_ZEN_API_KEY",
        "MODAL_TOKEN_SECRET",
        "GITHUB_TOKEN",
        "SBX_DATABASE_URL",
        "DATABASE_URL",
        "SBX_VAULT_KEYS",
    ],
)
def test_cloud_and_control_plane_inputs_are_stripped(key) -> None:
    assert _is_cloud_key(key)


def test_explicit_test_database_url_is_preserved() -> None:
    assert not _is_cloud_key("SBX_TEST_DATABASE_URL")


def test_probe(tmp_path) -> None:
    """Records the effective environment when run by the subprocess test below."""
    out = os.environ.get("SBX_CONFTEST_PROBE_OUT")
    if not out:
        pytest.skip("probe only runs inside test_scrub_preserves_test_database_url")
    keys = ("SBX_TEST_DATABASE_URL", "SBX_TEST_GITHUB_TOKEN", "MODAL_TOKEN_ID")
    Path(out).write_text(json.dumps({k: k in os.environ for k in keys}))


def test_scrub_preserves_test_database_url(tmp_path) -> None:
    out = tmp_path / "probe.json"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "SBX_CONFTEST_PROBE_OUT": str(out),
        "SBX_TEST_DATABASE_URL": "postgresql://postgres@127.0.0.1:1/postgres",
        "SBX_TEST_GITHUB_TOKEN": "REDACTED",
        "MODAL_TOKEN_ID": "REDACTED",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"{__file__}::test_probe"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert json.loads(out.read_text()) == {
        "SBX_TEST_DATABASE_URL": True,
        "SBX_TEST_GITHUB_TOKEN": False,
        "MODAL_TOKEN_ID": False,
    }
