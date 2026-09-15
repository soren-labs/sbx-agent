"""SOR-101 / SOR-55 / SOR-56: host credential env can never reach tests.

The original SOR-55/SOR-56 regression: a developer shell holding real
provider credentials (``CODEX_AUTH_JSON`` and friends) ran ``make test``;
cloud-free tests either injected those credentials into sandbox auth stores
or — worse — dumped token fragments into pytest output when assertions
failed.

This suite proves the autouse scrub in ``tests/conftest.py`` (plus the
cloud-free ``live_env`` belt-and-suspenders ``delenv``) holds across the
whole credential env universe. Only synthetic canaries are used.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

CANARY = "CANARY-SBX-TOKEN-0123456789abcdef"

# Env values a real developer shell plausibly exports. Every name must be
# invisible to every test (autouse scrub) — and therefore invisible to
# sandbox auth stores and to pytest output.
CANARY_ENV: dict[str, str] = {
    "CODEX_AUTH_JSON": json.dumps(
        {"tokens": {"access_token": CANARY, "refresh_token": f"{CANARY}-r"}}
    ),
    "CODEX_API_KEY": CANARY,
    "SBX_ACCOUNT_CREDENTIAL": json.dumps(
        {"provider": "devin", "files": {"cred.toml": f'k = "{CANARY}"'}}
    ),
    "SBX_ACCOUNT_ID": "canary-acct",
    "SBX_PROVIDER_API_KEY": CANARY,
    "MODAL_TOKEN_ID": CANARY,
    "MODAL_TOKEN_SECRET": CANARY,
    "OPENAI_API_KEY": CANARY,
    "XAI_API_KEY": CANARY,
    "ANTHROPIC_API_KEY": CANARY,
    "DEVIN_API_KEY": CANARY,
    "GROK_DEPLOYMENT_KEY": CANARY,
    "AGY_AUTH_TOKEN": CANARY,
    "GH_TOKEN": CANARY,
    "GITHUB_TOKEN": CANARY,
    "ACP_BACKEND": "canary",
    "WINDSURF_API_KEY": CANARY,
    "AWS_SECRET_ACCESS_KEY": CANARY,
    "CLOUDFLARE_API_TOKEN": CANARY,
    "SBX_V1_BOOTSTRAP_KEY": CANARY,
}


def test_autouse_scrub_removed_credential_env() -> None:
    """The autouse fixture runs before this test body — every credential
    name in the scrub universe must already be absent from ``os.environ``."""
    for key in CANARY_ENV:
        assert os.environ.get(key) is None, f"{key} survived the env scrub"


def test_host_canary_env_never_reaches_test_or_output() -> None:
    """Run the SOR-55 reproducer under a canary-laden parent env.

    ``test_item1`` only passes when the sandbox ``auth.json`` holds the
    ``REDACTED`` placeholder, so a clean run proves the canary never reached
    the sandbox; the output sweep proves that even a failing assertion
    cannot echo the canary into CI logs.
    """
    env = dict(os.environ)
    env.update(CANARY_ENV)
    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-x",
            "-q",
            "tests/integration/cloud_free/test_cloud_free_lifecycle.py"
            "::test_item1_init_writes_config_and_agents_without_secrets",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = res.stdout + res.stderr
    assert res.returncode == 0, f"child pytest failed:\n{output[-2000:]}"
    assert CANARY not in output
    assert "CANARY-SBX" not in output
