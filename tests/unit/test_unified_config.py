"""UnifiedConfig.from_env: required settings and the Resend sender guard."""

from __future__ import annotations

import base64

import pytest
from control.composition import UnifiedConfig

# All-zero keys: well-formed, never secret. Key format is validated at start-up.
BASE = {
    "SBX_DATABASE_URL": "postgresql://localhost/sbx",
    "SBX_VAULT_KEYS": "k1:" + base64.b64encode(bytes(32)).decode(),
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


@pytest.mark.parametrize(
    ("key", "value", "needle"),
    [
        ("SBX_VAULT_KEYS", "k1:REDACTED", "32 bytes"),
        ("SBX_VAULT_KEYS", "k1:not base64!", "not valid base64"),
        ("SBX_VAULT_KEYS", "REDACTED", "KID:BASE64KEY"),
        ("SBX_RUNTIME_MASTER_KEY", "zz" * 32, "hex"),
        ("SBX_RUNTIME_MASTER_KEY", "00" * 8, "at least 32 bytes"),
        ("SBX_EXECUTORS", "local,docker", "unknown executor backend(s): docker"),
        ("SBX_WORKER_THREADS", "0", "positive integer"),
    ],
)
def test_malformed_settings_are_refused_at_start_up_with_the_fix(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str, needle: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        _config(monkeypatch, **{key: value})
    assert needle in str(exc.value.code)
    assert value not in str(exc.value.code) or value in ("0",), "values are never echoed"


def test_missing_settings_point_at_server_init(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in BASE:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit) as exc:
        UnifiedConfig.from_env()
    message = str(exc.value.code)
    assert "sbx server init" in message
    assert "missing: SBX_DATABASE_URL, SBX_VAULT_KEYS, SBX_RUNTIME_MASTER_KEY" in message


def test_executor_list_ignores_empty_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _config(monkeypatch, SBX_EXECUTORS="local,").executors == ("local",)
