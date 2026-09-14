"""SOR-74: provider=devin selects sbx-runtime-devin image + account Secrets."""

from __future__ import annotations

from pathlib import Path

from control.backend import SandboxHandle, SandboxSpec
from control.backends.modal import (
    ModalBackend,
    _create_env,
    _devin_home_env,
    _resolve_image,
    _sandbox_secrets,
    _spec_provider,
)
from control.config import DEVIN_IMAGE_NAME, RUNTIME_IMAGE_NAME


class _FakeImage:
    calls: list[str] = []

    @staticmethod
    def from_name(name: str) -> tuple[str, str]:
        _FakeImage.calls.append(name)
        return ("image", name)


class _FakeSecret:
    @staticmethod
    def from_name(name: str) -> tuple[str, str]:
        return ("secret", name)

    @staticmethod
    def from_dict(data: dict) -> tuple[str, dict]:
        return ("dict", data)


class _FakeModal:
    Image = _FakeImage
    Secret = _FakeSecret


def test_spec_provider_defaults_to_codex() -> None:
    assert _spec_provider(SandboxSpec()) == "codex"
    assert _spec_provider(SandboxSpec(tags={"provider": "devin"})) == "devin"


def test_resolve_image_devin_uses_named_devin_image() -> None:
    _FakeImage.calls.clear()
    image = _resolve_image(_FakeModal, "devin")
    assert image == ("image", DEVIN_IMAGE_NAME)
    assert _FakeImage.calls == ["sbx-runtime-devin"]


def test_resolve_image_codex_unchanged() -> None:
    _FakeImage.calls.clear()
    image = _resolve_image(_FakeModal, "codex")
    assert image == ("image", RUNTIME_IMAGE_NAME)
    assert _FakeImage.calls == ["sbx-runtime"]


def test_sandbox_secrets_per_account() -> None:
    spec = SandboxSpec(tags={"provider": "devin"}, secrets=["sbx-acct-1", "sbx-extra"])
    secrets = _sandbox_secrets(_FakeModal, spec)
    assert secrets == [("secret", "sbx-acct-1"), ("secret", "sbx-extra")]


def test_sandbox_secrets_devin_without_spec_gets_none() -> None:
    spec = SandboxSpec(tags={"provider": "devin"})
    assert _sandbox_secrets(_FakeModal, spec) == []


def test_sandbox_secrets_codex_unchanged() -> None:
    secrets = _sandbox_secrets(_FakeModal, SandboxSpec())
    assert secrets == [("secret", "sbx-codex-auth")]


def test_create_env_devin_sets_home_xdg() -> None:
    env = _create_env(SandboxSpec(tags={"provider": "devin"}))
    assert env["SBX_WORK"] == "/work"
    assert env["CODEX_HOME"] == "/work/.codex"
    assert env["HOME"] == "/work/home"
    assert env["XDG_DATA_HOME"] == "/work/home/.local/share"
    assert env["XDG_CONFIG_HOME"] == "/work/home/.config"


def test_create_env_codex_has_no_home_override() -> None:
    env = _create_env(SandboxSpec())
    assert "HOME" not in env
    assert "XDG_DATA_HOME" not in env


def test_create_env_merges_spec_env() -> None:
    env = _create_env(SandboxSpec(env={"EXTRA": "1"}))
    assert env["EXTRA"] == "1"


def test_devin_home_env_matches_image_env() -> None:
    from runtime.image import devin_runtime_env

    assert _devin_home_env() == devin_runtime_env("/work")


def test_image_name_constant_in_sync() -> None:
    from runtime.image import DEVIN_IMAGE_NAME as RUNTIME_DEVIN_IMAGE_NAME

    assert DEVIN_IMAGE_NAME == RUNTIME_DEVIN_IMAGE_NAME


def test_exec_secrets_follow_spec_and_provider() -> None:
    backend = ModalBackend()
    backend._secrets_by_sandbox["sb-devin"] = ["sbx-acct-9"]
    devin = SandboxHandle(id="sb-devin", root=Path("/work"), tags={"provider": "devin"})
    assert backend._exec_secrets(_FakeModal, devin) == [("secret", "sbx-acct-9")]

    devin_no_secret = SandboxHandle(id="sb-d2", root=Path("/work"), tags={"provider": "devin"})
    assert backend._exec_secrets(_FakeModal, devin_no_secret) == []

    codex = SandboxHandle(id="sb-c", root=Path("/work"), tags={})
    assert backend._exec_secrets(_FakeModal, codex) == [("secret", "sbx-codex-auth")]
