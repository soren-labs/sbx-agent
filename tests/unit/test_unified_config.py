"""UnifiedConfig.from_env: required settings and the Resend sender guard."""

from __future__ import annotations

import pytest
from control.composition import UnifiedConfig

BASE = {
    "SBX_DATABASE_URL": "postgresql://localhost/sbx",
    "SBX_VAULT_KEYS": "k1:REDACTED",
    "SBX_RUNTIME_MASTER_KEY": "00" * 32,
}


def _config(monkeypatch: pytest.MonkeyPatch, **extra: str) -> UnifiedConfig:
    for key, value in {**BASE, **extra}.items():
        monkeypatch.setenv(key, value)
    return UnifiedConfig.from_env()


def test_local_mail_sink_needs_no_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(monkeypatch)
    assert config.resend_api_key is None
    assert config.mail_from == UnifiedConfig.mail_from


def test_resend_requires_explicit_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit, match="SBX_MAIL_FROM"):
        _config(monkeypatch, SBX_RESEND_API_KEY="REDACTED")


def test_resend_uses_configured_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(
        monkeypatch, SBX_RESEND_API_KEY="REDACTED", SBX_MAIL_FROM="SBX <no-reply@sbx.example>"
    )
    assert config.mail_from == "SBX <no-reply@sbx.example>"


def test_required_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SBX_VAULT_KEYS", raising=False)
    for key in ("SBX_DATABASE_URL", "SBX_RUNTIME_MASTER_KEY"):
        monkeypatch.setenv(key, BASE[key])
    with pytest.raises(SystemExit, match="required"):
        UnifiedConfig.from_env()
