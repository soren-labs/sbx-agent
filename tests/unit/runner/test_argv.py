"""Codex argv construction and child env (no subprocess)."""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.runner.codex import build_codex_argv, child_env


def test_first_turn_argv_matches_p0_and_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_BIN", "codex")
    argv = build_codex_argv(
        work=tmp_path,
        prompt="please do the task",
        thread_id=None,
        model="gpt-5.6-luna",
    )
    assert argv[:6] == [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-C",
        str(tmp_path),
    ]
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "-m" in argv
    assert argv[argv.index("-m") + 1] == "gpt-5.6-luna"
    assert argv[-1] == "please do the task"
    assert argv[-1] != "-"
    assert "resume" not in argv


def test_resume_argv_thread_id_then_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_BIN", "codex")
    argv = build_codex_argv(
        work=tmp_path,
        prompt="second turn",
        thread_id="01a09a36-b4fb-7f90-b96e-42adeefa05e0",
        model="gpt-5.6-luna",
    )
    assert argv[1:4] == ["exec", "resume", "--json"]
    assert "--skip-git-repo-check" in argv
    assert "-C" in argv
    assert argv[-2] == "01a09a36-b4fb-7f90-b96e-42adeefa05e0"
    assert argv[-1] == "second turn"
    assert argv[-1] != "-"
    assert "-m" not in argv


def test_child_env_drops_codex_auth_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_AUTH_JSON", '{"tokens":{"access_token":"REDACTED"}}')
    monkeypatch.setenv("SBX_PROVIDER_API_KEY", "SENTINEL")
    env = child_env(tmp_path, tmp_path / ".codex")
    assert "CODEX_AUTH_JSON" not in env
    assert env["SBX_PROVIDER_API_KEY"] == "SENTINEL"
    assert env["SBX_WORK"] == str(tmp_path)
    assert env["CODEX_HOME"] == str(tmp_path / ".codex")
