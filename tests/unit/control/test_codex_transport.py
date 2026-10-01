from control.config import remote_env_overlay
from control.sandbox_io import sandbox_env
from tests.unit.control.test_credential_scoping import _handle


def test_transport_fallback_reaches_codex_runner(monkeypatch):
    monkeypatch.setenv("SBX_CODEX_TRANSPORT", "exec")
    assert sandbox_env(_handle("codex")).get("SBX_CODEX_TRANSPORT") == "exec"


def test_transport_fallback_reaches_remote_control():
    assert remote_env_overlay({"SBX_CODEX_TRANSPORT": "exec"}).get("SBX_CODEX_TRANSPORT") == "exec"


def test_codex_transport_is_not_forwarded_to_other_providers(monkeypatch):
    monkeypatch.setenv("SBX_CODEX_TRANSPORT", "app-server")
    for provider in ("devin", "grok", "antigravity", "opencode"):
        assert "SBX_CODEX_TRANSPORT" not in sandbox_env(_handle(provider))
