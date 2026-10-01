"""RC namespace isolation on the control plane: env-resolved names.

A parallel deployment (``sbx-control-release01-rc``) relies on the remote
functions seeing the deploy-time ``SBX_*`` names — the ``env=`` overlay on
``control/modal_app.py`` — instead of the production contract defaults.
These tests pin the allowlist and every env-resolved name site.
"""

from __future__ import annotations

from control.backend import SandboxSpec
from control.backends.modal import _codex_secrets, _resolve_image, _sandbox_secrets
from control.config import (
    MODAL_APP_NAME,
    account_secret_prefix,
    app_secret_names,
    remote_env_overlay,
    selected_providers,
)


class _FakeImage:
    @staticmethod
    def from_name(name: str) -> tuple[str, str]:
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


def test_remote_env_overlay_only_forwards_allowlisted_names() -> None:
    env = {
        "SBX_SESSIONS_DICT": "rc-sessions",
        "SBX_TASKS_DICT": "rc-tasks",
        "SBX_REVISIONS_DICT": "rc-revisions",
        "SBX_RUN_ACTIVITY_DICT": "rc-activity",
        "SBX_IMAGE_GROK": "rc-runtime-grok",
        "SBX_ACCOUNT_SECRET_PREFIX": "rc-acct-",
        "SBX_PROVIDERS": "devin,grok",
        "SBX_DEVIN_ACCOUNT_ID": "devin-rc-1",
        # credential material — must never enter a function env
        "SBX_API_KEY": "REDACTED",
        "SBX_V1_BOOTSTRAP_KEY": "REDACTED",
        "SBX_BASIC_PASS": "REDACTED",
        "SBX_BASIC_USER": "sbx",
        "SBX_ACCOUNT_CREDENTIAL": '{"files": {}}',
        "CODEX_AUTH_JSON": "REDACTED",
        "SBX_LINEAR_API_KEY": "REDACTED",
        "UNRELATED": "nope",
    }
    out = remote_env_overlay(env, app_name="sbx-control-release01-rc")
    assert out["SBX_MODAL_APP_NAME"] == "sbx-control-release01-rc"
    assert out["SBX_SESSIONS_DICT"] == "rc-sessions"
    assert out["SBX_TASKS_DICT"] == "rc-tasks"
    assert out["SBX_REVISIONS_DICT"] == "rc-revisions"
    assert out["SBX_RUN_ACTIVITY_DICT"] == "rc-activity"
    assert out["SBX_IMAGE_GROK"] == "rc-runtime-grok"
    assert out["SBX_ACCOUNT_SECRET_PREFIX"] == "rc-acct-"
    assert out["SBX_PROVIDERS"] == "devin,grok"
    assert out["SBX_DEVIN_ACCOUNT_ID"] == "devin-rc-1"
    for denied in (
        "SBX_API_KEY",
        "SBX_V1_BOOTSTRAP_KEY",
        "SBX_BASIC_PASS",
        "SBX_BASIC_USER",
        "SBX_ACCOUNT_CREDENTIAL",
        "CODEX_AUTH_JSON",
        "SBX_LINEAR_API_KEY",
        "UNRELATED",
    ):
        assert denied not in out


def test_remote_env_overlay_defaults_to_production_app_name() -> None:
    out = remote_env_overlay({})
    assert out == {"SBX_MODAL_APP_NAME": MODAL_APP_NAME}


def test_remote_env_overlay_forwards_provider_set() -> None:
    """SOR-116: the remote app must see the enabled-provider set so it can
    mount/seed per provider instead of assuming codex."""
    out = remote_env_overlay({"SBX_PROVIDERS": "devin,grok"}, app_name="x")
    assert out["SBX_PROVIDERS"] == "devin,grok"


def test_account_secret_prefix_env(monkeypatch) -> None:
    monkeypatch.setenv("SBX_ACCOUNT_SECRET_PREFIX", "rc-acct-")
    assert account_secret_prefix() == "rc-acct-"
    monkeypatch.delenv("SBX_ACCOUNT_SECRET_PREFIX")
    assert account_secret_prefix() == "sbx-acct-"


def test_seeded_accounts_use_custom_prefix(monkeypatch) -> None:
    """``configure_v1_bootstrap`` must name RC account Secrets, not the
    production ``sbx-acct-*`` convention."""
    from control.api_v1.bootstrap import configure_v1_bootstrap
    from fastapi import FastAPI

    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", "sbx_" + "c" * 40)
    monkeypatch.setenv("SBX_ACCOUNT_SECRET_PREFIX", "rc-acct-")
    monkeypatch.setenv("SBX_PROVIDERS", "codex,devin,antigravity,grok,opencode")
    app = FastAPI()
    assert configure_v1_bootstrap(app) is True
    registry = app.state.account_registry
    for account_id in ("devin-1", "antigravity-1", "grok-1", "opencode-1"):
        account = registry.get(account_id)
        assert account is not None
        assert account.secret_name == f"rc-acct-{account_id}"
        assert not account.secret_name.startswith("sbx-acct-")


def test_codex_secret_name_env(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_AUTH_JSON", raising=False)
    monkeypatch.setenv("SBX_CODEX_SECRET_NAME", "rc-codex-auth")
    assert _codex_secrets(_FakeModal) == [("secret", "rc-codex-auth")]


def test_provider_image_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("SBX_IMAGE_GROK", "rc-runtime-grok")
    monkeypatch.setenv("SBX_IMAGE_DEVIN", "rc-runtime-devin")
    assert _resolve_image(_FakeModal, "grok") == ("image", "rc-runtime-grok")
    assert _resolve_image(_FakeModal, "devin") == ("image", "rc-runtime-devin")
    # unset providers keep contract defaults
    assert _resolve_image(_FakeModal, "antigravity") == ("image", "sbx-runtime-antigravity")


def test_codex_image_env_override(monkeypatch) -> None:
    monkeypatch.setenv("SBX_IMAGE_CODEX", "rc-runtime")
    assert _resolve_image(_FakeModal, "codex") == ("image", "rc-runtime")


def test_app_secret_names_gate_codex_on_selected_providers() -> None:
    """The shared Codex Secret mounts only when codex is a selected
    provider — an unselected credential must not block a deploy (SOR-115)."""
    assert app_secret_names({"SBX_PROVIDERS": "codex"}) == [
        "sbx-codex-auth",
        "sbx-basic-auth",
        "sbx-v1-bootstrap",
    ]
    assert app_secret_names({"SBX_PROVIDERS": "devin,grok"}) == [
        "sbx-basic-auth",
        "sbx-v1-bootstrap",
    ]
    # SOR-210: the default is platform-only — no provider credential
    # Secret mounts when SBX_PROVIDERS is unset.
    assert app_secret_names({}) == [
        "sbx-basic-auth",
        "sbx-v1-bootstrap",
    ]
    # Renamed secrets honor the configured names.
    assert app_secret_names(
        {
            "SBX_PROVIDERS": "codex",
            "SBX_CODEX_SECRET_NAME": "rc-codex-auth",
            "SBX_BASIC_SECRET_NAME": "rc-basic",
        }
    ) == ["rc-codex-auth", "rc-basic", "sbx-v1-bootstrap"]


def test_selected_providers_parses_csv() -> None:
    # SOR-210: unset means platform-only (no providers), not codex.
    assert selected_providers({}) == ()
    assert selected_providers({"SBX_PROVIDERS": " devin , grok "}) == ("devin", "grok")


def test_app_secret_names_github_bridge_is_opt_in() -> None:
    """SOR-117: a named GitHub Secret mounts on the control app only when
    the operator armed the gate AND named the Secret — a remote deploy gets
    GH_TOKEN in the function env without baking the value anywhere."""
    base = {"SBX_PROVIDERS": "devin"}
    # Secret named but gate off → not mounted.
    assert "sbx-github" not in app_secret_names({**base, "SBX_GITHUB_SECRET_NAME": "sbx-github"})
    # Gate on but no Secret named → nothing extra mounts.
    assert "sbx-github" not in app_secret_names({**base, "SBX_GITHUB_EPHEMERAL": "1"})
    # Both → the named Secret mounts on the control app.
    assert app_secret_names(
        {
            **base,
            "SBX_GITHUB_EPHEMERAL": "1",
            "SBX_GITHUB_SECRET_NAME": "sbx-github",
        }
    ) == ["sbx-basic-auth", "sbx-v1-bootstrap", "sbx-github"]


def test_custom_codex_secret_name_still_filtered_from_account_sandboxes(
    monkeypatch,
) -> None:
    """The codex-auth exclusion must catch the renamed Secret too — an RC
    codex Secret passed to an account-provider sandbox is still stripped."""
    monkeypatch.setenv("SBX_CODEX_SECRET_NAME", "rc-codex-auth")
    spec = SandboxSpec(tags={"provider": "grok"}, secrets=["rc-codex-auth", "rc-acct-grok-1"])
    secrets = _sandbox_secrets(_FakeModal, spec)
    assert ("secret", "rc-codex-auth") not in secrets
    assert ("secret", "rc-acct-grok-1") in secrets
