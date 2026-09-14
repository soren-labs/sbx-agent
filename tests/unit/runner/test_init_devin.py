"""``runner init --provider devin``: credential blob restore + session meta (SOR-74)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from tests.unit.runner.conftest import init_runner, load_json, run_runner

DEVIN_MODEL = "swe-2-high"
CRED_TOML = (
    'windsurf_api_key = "REDACTED"\n'
    'api_server_url = "https://server.codeium.com"\n'
    'devin_webapp_host = "app.devin.ai"\n'
    'devin_api_url = "https://api.devin.ai"\n'
)


def _devin_env(runner_env: dict[str, str]) -> dict[str, str]:
    runner_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {
            "provider": "devin",
            "files": {".local/share/devin/credentials.toml": CRED_TOML},
        }
    )
    runner_env["SBX_ACCOUNT_ID"] = "acct-devin-1"
    return runner_env


def test_init_devin_restores_credentials_toml(work: Path, runner_env: dict[str, str]) -> None:
    env = _devin_env(runner_env)
    result = run_runner(["init", "--provider", "devin", "--model", DEVIN_MODEL], env)
    assert result.returncode == 0, result.stderr

    cred = work / "home" / ".local" / "share" / "devin" / "credentials.toml"
    assert cred.is_file()
    assert cred.read_text(encoding="utf-8") == CRED_TOML
    assert stat.S_IMODE(cred.stat().st_mode) == 0o600

    session = load_json(work / "session.json")
    assert session["provider"] == "devin"
    assert session["account_id"] == "acct-devin-1"
    assert session["model"] == DEVIN_MODEL
    assert session["native_session_id"] is None

    # The blob env var name/value must not leak into events or session files.
    assert CRED_TOML not in (work / "events.jsonl").read_text(encoding="utf-8")
    assert "SBX_ACCOUNT_CREDENTIAL" not in (work / "session.json").read_text(encoding="utf-8")


def test_init_devin_rejects_mismatched_blob_provider(
    work: Path, runner_env: dict[str, str]
) -> None:
    runner_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {"provider": "codex", "files": {".codex/auth.json": "{}"}}
    )
    result = run_runner(["init", "--provider", "devin", "--model", DEVIN_MODEL], runner_env)
    assert result.returncode != 0
    assert "provider" in result.stderr.lower()
    cred = work / "home" / ".local" / "share" / "devin" / "credentials.toml"
    assert not cred.exists()


def test_init_devin_rejects_traversal_path(work: Path, runner_env: dict[str, str]) -> None:
    runner_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {"provider": "devin", "files": {"../escape.txt": "x"}}
    )
    result = run_runner(["init", "--provider", "devin", "--model", DEVIN_MODEL], runner_env)
    assert result.returncode != 0
    assert not (work / "escape.txt").exists()


def test_init_devin_without_blob_succeeds(work: Path, runner_env: dict[str, str]) -> None:
    runner_env.pop("SBX_ACCOUNT_CREDENTIAL", None)
    result = run_runner(["init", "--provider", "devin", "--model", DEVIN_MODEL], runner_env)
    assert result.returncode == 0, result.stderr
    session = load_json(work / "session.json")
    assert session["provider"] == "devin"
    assert session["account_id"] is None


def test_init_codex_blob_restores_under_home(work: Path, runner_env: dict[str, str]) -> None:
    runner_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {
            "provider": "codex",
            "files": {".codex/auth.json": '{"tokens":{"access_token":"REDACTED"}}'},
        }
    )
    init_runner(runner_env)  # --provider defaults to codex
    restored = work / "home" / ".codex" / "auth.json"
    assert restored.is_file()
    assert stat.S_IMODE(restored.stat().st_mode) == 0o600


def test_init_codex_default_unchanged_without_blob(work: Path, runner_env: dict[str, str]) -> None:
    runner_env.pop("SBX_ACCOUNT_CREDENTIAL", None)
    init_runner(runner_env)
    session = load_json(work / "session.json")
    assert session["provider"] == "codex"
    assert session["auth"] == "auth_json"
    assert (work / ".codex" / "auth.json").is_file()
