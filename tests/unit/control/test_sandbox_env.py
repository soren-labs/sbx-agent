"""sandbox_env forwards CODEX_AUTH_JSON so exec env= cannot hide a named Secret."""

from __future__ import annotations

from pathlib import Path

from control.backend import SandboxHandle
from control.sandbox_io import sandbox_env


def test_sandbox_env_forwards_codex_auth_json(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_AUTH_JSON", "REDACTED")
    handle = SandboxHandle(id="sb", root=Path("/work"), tags={})
    env = sandbox_env(handle)
    assert env["CODEX_AUTH_JSON"] == "REDACTED"
    assert env["SBX_WORK"] == "/work"
    assert env["CODEX_HOME"] == "/work/.codex"


def test_sandbox_env_omits_auth_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_AUTH_JSON", raising=False)
    handle = SandboxHandle(id="sb", root=Path("/work"), tags={})
    env = sandbox_env(handle)
    assert "CODEX_AUTH_JSON" not in env


def test_sandbox_env_extra_overrides(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_AUTH_JSON", "REDACTED")
    handle = SandboxHandle(id="sb", root=Path("/work"), tags={})
    env = sandbox_env(handle, extra={"PYTHONUNBUFFERED": "0"})
    assert env["PYTHONUNBUFFERED"] == "0"
    assert env["CODEX_AUTH_JSON"] == "REDACTED"
