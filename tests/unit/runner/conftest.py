"""Shared helpers for runner unit tests. No cloud credentials."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MODEL = "gpt-5.6-luna"
DEFAULT_THREAD = "01a09a36-b4fb-7f90-b96e-42adeefa05e0"
USAGE_FIELDS = {
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
}


@pytest.fixture
def work(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    return root


@pytest.fixture
def runner_env(work: Path, fake_codex: Path, repo_root: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "CODEX_AUTH_JSON"}
    env.pop("SBX_PROVIDER_API_KEY", None)
    env.pop("SBX_PROVIDER_BASE_URL", None)
    env.update(
        {
            "SBX_WORK": str(work),
            "CODEX_HOME": str(work / ".codex"),
            "CODEX_BIN": str(fake_codex),
            "PYTHONPATH": str(repo_root),
            "PYTHONUNBUFFERED": "1",
            "FAKE_CODEX_SCENARIO": "success",
            "SBX_BACKEND": "local",
        }
    )
    return env


def run_runner(
    args: list[str],
    env: dict[str, str],
    *,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "runtime.runner", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def init_runner(
    env: dict[str, str],
    *,
    auth: str = "auth_json",
    model: str = MODEL,
) -> subprocess.CompletedProcess[str]:
    result = run_runner(["init", "--auth", auth, "--model", model], env)
    assert result.returncode == 0, result.stderr
    return result


def write_message(work: Path, text: str = "hello from test") -> Path:
    path = work / "msg.md"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def parsed_events(work: Path) -> list[dict]:
    out: list[dict] = []
    path = work / "events.jsonl"
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            out.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return out
