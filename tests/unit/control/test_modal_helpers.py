"""Helpers on control.backends.modal — import the module, never call ModalBackend."""

from __future__ import annotations

from control.backends.modal import _codex_secrets, _is_sandbox_gone
from control.config import CODEX_SECRET_NAME

ConflictError = type("ConflictError", (Exception,), {})
NotFoundError = type("NotFoundError", (Exception,), {})


class _FakeSecret:
    def __init__(self, kind: str, payload: object) -> None:
        self.kind = kind
        self.payload = payload

    @staticmethod
    def from_dict(data: dict[str, str]) -> _FakeSecret:
        return _FakeSecret("dict", data)

    @staticmethod
    def from_name(name: str) -> _FakeSecret:
        return _FakeSecret("name", name)


class _FakeModal:
    Secret = _FakeSecret


def test_sandbox_gone_treats_conflict_and_not_found() -> None:
    assert _is_sandbox_gone(ConflictError("shutting down"))
    assert _is_sandbox_gone(NotFoundError("gone"))
    assert not _is_sandbox_gone(RuntimeError("other"))
    assert not _is_sandbox_gone(ValueError("nope"))


def test_codex_secrets_from_dict_when_env_set(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_AUTH_JSON", "REDACTED")
    secrets = _codex_secrets(_FakeModal)
    assert len(secrets) == 1
    assert secrets[0].kind == "dict"
    assert secrets[0].payload == {"CODEX_AUTH_JSON": "REDACTED"}


def test_codex_secrets_from_name_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_AUTH_JSON", raising=False)
    secrets = _codex_secrets(_FakeModal)
    assert len(secrets) == 1
    assert secrets[0].kind == "name"
    assert secrets[0].payload == CODEX_SECRET_NAME
    assert secrets[0].payload == "sbx-codex-auth"
