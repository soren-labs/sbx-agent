"""SOR-80: ambient credential forwarding is scoped by provider/account.

Covers ``control.sandbox_io.sandbox_env`` (exec env) and
``control.backends.modal`` secret resolution (create/exec/kill) without any
Modal SDK import — helpers take a fake ``modal`` module like the neighboring
suites.
"""

from __future__ import annotations

import json
from pathlib import Path

from control.backend import SandboxHandle, SandboxSpec
from control.backends.modal import (
    ModalBackend,
    ModalProcess,
    _account_secrets,
    _sandbox_secrets,
)
from control.config import CODEX_SECRET_NAME
from control.sandbox_io import sandbox_env


def _blob(provider: str, files: dict | None = None) -> str:
    return json.dumps({"provider": provider, "files": files or {"cred": "REDACTED"}})


def _handle(provider: str | None = None, account_id: str | None = None) -> SandboxHandle:
    tags: dict[str, str] = {}
    if provider is not None:
        tags["provider"] = provider
    if account_id is not None:
        tags["account_id"] = account_id
    return SandboxHandle(id="sb", root=Path("/work"), tags=tags)


class _FakeSecret:
    @staticmethod
    def from_name(name: str) -> tuple[str, str]:
        return ("secret", name)

    @staticmethod
    def from_dict(data: dict) -> tuple[str, dict]:
        return ("dict", data)


class _FakeModal:
    Secret = _FakeSecret


# ------------------------------------------------------------ sandbox_env


def test_sandbox_env_wrong_provider_blob_not_forwarded(monkeypatch) -> None:
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", _blob("devin"))
    env = sandbox_env(_handle("grok", "grok-1"))
    assert "SBX_ACCOUNT_CREDENTIAL" not in env


def test_sandbox_env_matching_provider_blob_forwarded(monkeypatch) -> None:
    blob = _blob("grok")
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", blob)
    env = sandbox_env(_handle("grok", "grok-1"))
    assert env["SBX_ACCOUNT_CREDENTIAL"] == blob


def test_sandbox_env_malformed_blob_not_forwarded(monkeypatch) -> None:
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", "REDACTED-not-json")
    env = sandbox_env(_handle("devin", "devin-1"))
    assert "SBX_ACCOUNT_CREDENTIAL" not in env


def test_sandbox_env_account_mismatch_not_forwarded(monkeypatch) -> None:
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", _blob("devin"))
    monkeypatch.setenv("SBX_ACCOUNT_ID", "devin-A")
    env = sandbox_env(_handle("devin", "devin-B"))
    assert "SBX_ACCOUNT_CREDENTIAL" not in env
    assert "SBX_ACCOUNT_ID" not in env


def test_sandbox_env_account_match_forwards_id_and_blob(monkeypatch) -> None:
    blob = _blob("devin")
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", blob)
    monkeypatch.setenv("SBX_ACCOUNT_ID", "devin-1")
    env = sandbox_env(_handle("devin", "devin-1"))
    assert env["SBX_ACCOUNT_CREDENTIAL"] == blob
    assert env["SBX_ACCOUNT_ID"] == "devin-1"


def test_sandbox_env_codex_auth_excluded_for_non_codex(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_AUTH_JSON", "REDACTED")
    for provider in ("devin", "antigravity", "grok", "opencode"):
        env = sandbox_env(_handle(provider, f"{provider}-1"))
        assert "CODEX_AUTH_JSON" not in env
        assert "CODEX_BIN" not in env
    env = sandbox_env(_handle("codex"))
    assert env["CODEX_AUTH_JSON"] == "REDACTED"


def test_sandbox_env_provider_api_vars_codex_only(monkeypatch) -> None:
    monkeypatch.setenv("SBX_PROVIDER_API_KEY", "REDACTED")
    monkeypatch.setenv("SBX_PROVIDER_BASE_URL", "https://example.invalid/v1")
    env = sandbox_env(_handle("grok", "grok-1"))
    assert "SBX_PROVIDER_API_KEY" not in env
    assert "SBX_PROVIDER_BASE_URL" not in env
    env = sandbox_env(_handle("codex"))
    assert env["SBX_PROVIDER_API_KEY"] == "REDACTED"
    assert env["SBX_PROVIDER_BASE_URL"] == "https://example.invalid/v1"


def test_sandbox_env_fake_scenarios_scoped_per_provider(monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "hang")
    monkeypatch.setenv("FAKE_GROK_SCENARIO", "success")
    grok_env = sandbox_env(_handle("grok", "grok-1"))
    assert grok_env["FAKE_GROK_SCENARIO"] == "success"
    assert "FAKE_CODEX_SCENARIO" not in grok_env
    codex_env = sandbox_env(_handle("codex"))
    assert codex_env["FAKE_CODEX_SCENARIO"] == "hang"
    assert "FAKE_GROK_SCENARIO" not in codex_env


def test_sandbox_env_opencode_bin_scoped(monkeypatch) -> None:
    """``OPENCODE_BIN`` (local fast-path binary override) forwards only to
    opencode sandboxes."""
    monkeypatch.setenv("OPENCODE_BIN", "/opt/fakes/fake_opencode.py")
    env = sandbox_env(_handle("opencode", "opencode-1"))
    assert env["OPENCODE_BIN"] == "/opt/fakes/fake_opencode.py"
    for provider in ("codex", "grok", "devin"):
        assert "OPENCODE_BIN" not in sandbox_env(_handle(provider))


def test_sandbox_env_modal_handle_pins_image_pythonpath(monkeypatch, tmp_path) -> None:
    """A deployed control function's ``PYTHONPATH`` (Modal's mount root)
    must never reach the sandbox exec — it would shadow the image's
    ``/opt/sbx`` and ``python -m runtime.runner`` fails to resolve.
    Non-local handles get the contract value; local roots keep host
    forwarding for ``LocalProcessBackend``."""
    monkeypatch.setenv("PYTHONPATH", "/modal/mount/root")
    remote = sandbox_env(_handle("codex"))  # root=/work does not exist locally
    assert remote["PYTHONPATH"] == "/opt/sbx"

    local_root = tmp_path / "work"
    local_root.mkdir()
    local = sandbox_env(SandboxHandle(id="sb", root=local_root, tags={"provider": "codex"}))
    assert local["PYTHONPATH"] == "/modal/mount/root"


# ------------------------------------------------------- modal secret rule


def test_named_secret_authoritative_over_ambient_blob(monkeypatch) -> None:
    for provider in ("devin", "antigravity", "grok", "opencode"):
        monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", _blob(provider))
        spec = SandboxSpec(tags={"provider": provider}, secrets=["sbx-acct-1"])
        assert _sandbox_secrets(_FakeModal, spec) == [("secret", "sbx-acct-1")]


def test_ambient_blob_wrong_provider_not_attached(monkeypatch) -> None:
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", _blob("devin"))
    spec = SandboxSpec(tags={"provider": "grok"})
    assert _sandbox_secrets(_FakeModal, spec) == []


def test_ambient_blob_malformed_not_attached(monkeypatch) -> None:
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", "REDACTED-not-json")
    spec = SandboxSpec(tags={"provider": "grok"})
    assert _sandbox_secrets(_FakeModal, spec) == []


def test_ambient_blob_fallback_preserved_without_named_secret(monkeypatch) -> None:
    blob = _blob("grok")
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", blob)
    spec = SandboxSpec(tags={"provider": "grok"})
    assert _sandbox_secrets(_FakeModal, spec) == [("dict", {"SBX_ACCOUNT_CREDENTIAL": blob})]
    assert _account_secrets(_FakeModal, "grok") == [("dict", {"SBX_ACCOUNT_CREDENTIAL": blob})]


def test_exec_secrets_named_authoritative_over_ambient_blob(monkeypatch) -> None:
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", _blob("antigravity"))
    backend = ModalBackend()
    backend._secrets_by_sandbox["sb-agy"] = ["sbx-acct-7"]
    handle = SandboxHandle(id="sb-agy", root=Path("/work"), tags={"provider": "antigravity"})
    assert backend._exec_secrets(_FakeModal, handle) == [("secret", "sbx-acct-7")]


def test_kill_exec_secrets_named_authoritative(monkeypatch) -> None:
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", _blob("grok"))

    class _Proc:
        stdout = iter(())

    proc = ModalProcess(
        _Proc(),
        sandbox_id="sb-grok",
        pid_file="/tmp/x.pid",
        secret_names=("sbx-acct-3",),
        provider="grok",
    )
    assert proc._exec_secrets(_FakeModal) == [("secret", "sbx-acct-3")]


def test_devin_aux_bridges_stack_with_named_secret_but_not_blob(monkeypatch) -> None:
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", _blob("devin"))
    monkeypatch.setenv("SBX_GITHUB_EPHEMERAL", "1")
    monkeypatch.setenv("GH_TOKEN", "REDACTED_GITHUB")
    spec = SandboxSpec(tags={"provider": "devin"}, secrets=["sbx-acct-1"])
    secrets = _sandbox_secrets(_FakeModal, spec)
    assert secrets[0] == ("secret", "sbx-acct-1")
    assert ("dict", {"SBX_ACCOUNT_CREDENTIAL": _blob("devin")}) not in secrets
    assert ("dict", {"GH_TOKEN": "REDACTED_GITHUB", "GITHUB_TOKEN": "REDACTED_GITHUB"}) in secrets


def test_sandbox_env_extra_cannot_reintroduce_foreign_credentials(monkeypatch) -> None:
    handle = SandboxHandle(
        id="sb-grok-extra",
        root=Path("/work"),
        tags={"provider": "grok", "account_id": "grok-1"},
    )
    env = sandbox_env(
        handle,
        {
            "CODEX_AUTH_JSON": "should-not-pass",
            "SBX_PROVIDER_API_KEY": "should-not-pass",
            "SBX_PROVIDER_BASE_URL": "https://example.invalid",
            "SBX_ACCOUNT_ID": "devin-1",
            "SBX_ACCOUNT_CREDENTIAL": _blob("devin"),
        },
    )
    assert "CODEX_AUTH_JSON" not in env
    assert "SBX_PROVIDER_API_KEY" not in env
    assert "SBX_PROVIDER_BASE_URL" not in env
    assert "SBX_ACCOUNT_ID" not in env
    assert "SBX_ACCOUNT_CREDENTIAL" not in env


def test_account_provider_never_mounts_codex_named_secret() -> None:
    spec = SandboxSpec(tags={"provider": "grok"}, secrets=[CODEX_SECRET_NAME])
    secrets = _sandbox_secrets(_FakeModal, spec)
    assert ("secret", CODEX_SECRET_NAME) not in secrets
