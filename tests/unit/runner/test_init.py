"""runner init: config.toml, auth.json, layout, AGENTS.md."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from tests.unit.runner.conftest import MODEL, init_runner, load_json, run_runner


def test_init_auth_json_from_env(work: Path, runner_env: dict[str, str]) -> None:
    payload = {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": "REDACTED",
            "refresh_token": "REDACTED",
            "id_token": "REDACTED",
        },
    }
    runner_env["CODEX_AUTH_JSON"] = json.dumps(payload)
    init_runner(runner_env)

    config = (work / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert f'model = "{MODEL}"' in config
    assert 'approval_policy = "never"' in config
    assert 'sandbox_mode = "danger-full-access"' in config
    assert "CODEX_AUTH_JSON" in config
    assert "SBX_PROVIDER_API_KEY" in config
    assert "[model_providers" not in config

    auth_path = work / ".codex" / "auth.json"
    assert auth_path.is_file()
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600
    written = json.loads(auth_path.read_text(encoding="utf-8"))
    assert written["auth_mode"] == "chatgpt"
    assert written["tokens"]["access_token"] == "REDACTED"

    agents = (work / "AGENTS.md").read_text(encoding="utf-8")
    assert "/work" in agents
    assert "confirmation" in agents.lower() or "ask" in agents.lower()
    assert "plan" in agents.lower()

    session = load_json(work / "session.json")
    assert session["codex_session_id"] is None
    assert session["turn"] == 0
    assert session["pid"] is None
    assert session["model"] == MODEL
    assert (work / "inbox").is_dir()
    assert (work / "turns").is_dir()
    assert (work / "events.jsonl").is_file()
    assert (work / "events.jsonl").read_text(encoding="utf-8") == ""


def test_init_auth_json_copies_work_auth_when_env_missing(
    work: Path, runner_env: dict[str, str]
) -> None:
    runner_env.pop("CODEX_AUTH_JSON", None)
    src = {"auth_mode": "chatgpt", "tokens": {"access_token": "REDACTED"}}
    (work / "auth.json").write_text(json.dumps(src), encoding="utf-8")
    init_runner(runner_env)
    dest = json.loads((work / ".codex" / "auth.json").read_text(encoding="utf-8"))
    assert dest["tokens"]["access_token"] == "REDACTED"


def test_init_auth_json_placeholder_without_source(
    work: Path, runner_env: dict[str, str]
) -> None:
    runner_env.pop("CODEX_AUTH_JSON", None)
    init_runner(runner_env)
    dest = json.loads((work / ".codex" / "auth.json").read_text(encoding="utf-8"))
    assert dest["tokens"]["access_token"] == "REDACTED"
    assert stat.S_IMODE((work / ".codex" / "auth.json").stat().st_mode) == 0o600


def test_init_provider_writes_model_providers_not_the_key(
    work: Path, runner_env: dict[str, str]
) -> None:
    sentinel = "SENTINEL_PROVIDER_KEY_DO_NOT_STORE"
    runner_env["SBX_PROVIDER_API_KEY"] = sentinel
    runner_env["SBX_PROVIDER_BASE_URL"] = "https://example.invalid/v1"
    init_runner(runner_env, auth="provider")

    config = (work / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "[model_providers.sbx]" in config
    assert 'env_key = "SBX_PROVIDER_API_KEY"' in config
    assert "https://example.invalid/v1" in config
    assert sentinel not in config
    tree = "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in work.rglob("*") if p.is_file()
    )
    assert sentinel not in tree

    auth = json.loads((work / ".codex" / "auth.json").read_text(encoding="utf-8"))
    assert auth["auth_mode"] == "provider"
    assert stat.S_IMODE((work / ".codex" / "auth.json").stat().st_mode) == 0o600


def test_init_auth_defaults_to_auth_json(work: Path, runner_env: dict[str, str]) -> None:
    result = run_runner(["init", "--model", MODEL], runner_env)
    assert result.returncode == 0, result.stderr
    session = load_json(work / "session.json")
    assert session["auth"] == "auth_json"
    config = (work / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "[model_providers" not in config
