"""SOR-62/SOR-80/SOR-96: provider=antigravity / grok / opencode select named CLI images."""

from __future__ import annotations

import json
from pathlib import Path

from control.backend import SandboxHandle, SandboxSpec
from control.backends.modal import (
    ModalBackend,
    _create_env,
    _resolve_image,
    _sandbox_secrets,
    _spec_provider,
)
from control.config import (
    ANTIGRAVITY_IMAGE_NAME,
    DEVIN_IMAGE_NAME,
    GROK_IMAGE_NAME,
    OPENCODE_IMAGE_NAME,
    RUNTIME_IMAGE_NAME,
)


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


def test_spec_provider_reads_antigravity_and_grok() -> None:
    assert _spec_provider(SandboxSpec(tags={"provider": "antigravity"})) == "antigravity"
    assert _spec_provider(SandboxSpec(tags={"provider": "grok"})) == "grok"


def test_resolve_image_antigravity_uses_named_image() -> None:
    _FakeImage.calls.clear()
    image = _resolve_image(_FakeModal, "antigravity")
    assert image == ("image", ANTIGRAVITY_IMAGE_NAME)
    assert _FakeImage.calls == ["sbx-runtime-antigravity"]


def test_resolve_image_grok_uses_named_image() -> None:
    _FakeImage.calls.clear()
    image = _resolve_image(_FakeModal, "grok")
    assert image == ("image", GROK_IMAGE_NAME)
    assert _FakeImage.calls == ["sbx-runtime-grok"]


def test_resolve_image_opencode_uses_named_image() -> None:
    _FakeImage.calls.clear()
    image = _resolve_image(_FakeModal, "opencode")
    assert image == ("image", OPENCODE_IMAGE_NAME)
    assert _FakeImage.calls == ["sbx-runtime-opencode"]


def test_resolve_image_codex_and_devin_unchanged() -> None:
    _FakeImage.calls.clear()
    assert _resolve_image(_FakeModal, "codex") == ("image", RUNTIME_IMAGE_NAME)
    assert _resolve_image(_FakeModal, "devin") == ("image", DEVIN_IMAGE_NAME)
    assert _FakeImage.calls == ["sbx-runtime", "sbx-runtime-devin"]


def test_sandbox_secrets_per_account_no_codex_fallback() -> None:
    for provider in ("antigravity", "grok", "opencode"):
        spec = SandboxSpec(tags={"provider": provider}, secrets=["sbx-acct-1"])
        secrets = _sandbox_secrets(_FakeModal, spec)
        assert secrets[0] == ("secret", "sbx-acct-1")
        assert ("secret", "sbx-codex-auth") not in secrets


def test_sandbox_secrets_account_provider_without_spec_gets_none(monkeypatch) -> None:
    monkeypatch.delenv("SBX_ACCOUNT_CREDENTIAL", raising=False)
    monkeypatch.delenv("SBX_ACCOUNT_CREDENTIAL_FILE", raising=False)
    for provider in ("antigravity", "grok", "opencode"):
        spec = SandboxSpec(tags={"provider": provider})
        assert _sandbox_secrets(_FakeModal, spec) == []


def test_account_ephemeral_blob_passthrough(monkeypatch) -> None:
    for provider in ("antigravity", "grok", "opencode"):
        blob = json.dumps({"provider": provider, "files": {"cred": "REDACTED"}})
        monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", blob)
        spec = SandboxSpec(tags={"provider": provider})
        assert _sandbox_secrets(_FakeModal, spec) == [("dict", {"SBX_ACCOUNT_CREDENTIAL": blob})]


def test_credential_file_wrapped_with_provider_relpath(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("SBX_ACCOUNT_CREDENTIAL", raising=False)
    cred = tmp_path / "cred"
    cred.write_text("REDACTED", encoding="utf-8")
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL_FILE", str(cred))
    expected = {
        "antigravity": ".gemini/antigravity-cli/antigravity-oauth-token",
        "grok": ".grok/auth.json",
        "opencode": ".local/share/opencode/auth.json",
    }
    for provider, relpath in expected.items():
        spec = SandboxSpec(tags={"provider": provider})
        payload = _sandbox_secrets(_FakeModal, spec)[0][1]
        blob = json.loads(payload["SBX_ACCOUNT_CREDENTIAL"])
        assert blob["provider"] == provider
        assert blob["files"] == {relpath: "REDACTED"}


def test_create_env_account_providers_set_home() -> None:
    for provider in ("antigravity", "grok"):
        env = _create_env(SandboxSpec(tags={"provider": provider}))
        assert env["SBX_WORK"] == "/work"
        assert env["CODEX_HOME"] == "/work/.codex"
        assert env["HOME"] == "/work/home"
        assert "XDG_DATA_HOME" not in env


def test_create_env_opencode_pins_home_and_xdg() -> None:
    """SOR-96: opencode auth.json is an XDG data file, so the sandbox gets
    the same HOME+XDG pinning as devin."""
    env = _create_env(SandboxSpec(tags={"provider": "opencode"}))
    assert env["HOME"] == "/work/home"
    assert env["XDG_DATA_HOME"] == "/work/home/.local/share"
    assert env["XDG_CONFIG_HOME"] == "/work/home/.config"


def test_exec_secrets_follow_account_provider(monkeypatch) -> None:
    monkeypatch.delenv("SBX_ACCOUNT_CREDENTIAL", raising=False)
    monkeypatch.delenv("SBX_ACCOUNT_CREDENTIAL_FILE", raising=False)
    backend = ModalBackend()
    backend._secrets_by_sandbox["sb-agy"] = ["sbx-acct-7"]
    agy = SandboxHandle(id="sb-agy", root=Path("/work"), tags={"provider": "antigravity"})
    assert backend._exec_secrets(_FakeModal, agy) == [("secret", "sbx-acct-7")]

    grok = SandboxHandle(id="sb-g", root=Path("/work"), tags={"provider": "grok"})
    assert backend._exec_secrets(_FakeModal, grok) == []


def test_image_name_constants_in_sync() -> None:
    from runtime.image import AGY_IMAGE_NAME
    from runtime.image import GROK_IMAGE_NAME as RT_GROK
    from runtime.image import OPENCODE_IMAGE_NAME as RT_OPENCODE

    assert ANTIGRAVITY_IMAGE_NAME == AGY_IMAGE_NAME == "sbx-runtime-antigravity"
    assert GROK_IMAGE_NAME == RT_GROK == "sbx-runtime-grok"
    assert OPENCODE_IMAGE_NAME == RT_OPENCODE == "sbx-runtime-opencode"
