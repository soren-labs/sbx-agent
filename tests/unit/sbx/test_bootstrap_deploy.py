"""``sbx deploy``: idempotent pipeline, secrets wired, actionable failures."""

from __future__ import annotations

import json

import pytest
from sbx.config import BootstrapConfig, key_path, load
from sbx.deploy import deploy, read_deploy_state
from sbx.errors import BootstrapError
from sbx.keys import fingerprint, read_key
from sbx_fakes import FakePlane, make_cfg, make_env, make_v1


def _deploy(tmp_path, plane, *, env=None, config=None, **kwargs):
    env = env or make_env(tmp_path)
    cfg = make_cfg(tmp_path, env=env, config=config)
    transport, http = make_v1()
    report = deploy(
        cfg,
        plane,
        env=env,
        transport=transport,
        sleep=lambda s: None,
        **kwargs,
    )
    return report, env, http


def test_deploy_happy_path(tmp_path) -> None:
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    report, env, _ = _deploy(tmp_path, plane)

    token = read_key(key_path(env))
    assert token is not None
    # control only ever receives the token inside the Secret env; storage is hash-only
    assert plane.secrets["sbx-v1-bootstrap"]["SBX_V1_BOOTSTRAP_KEY"] == token
    assert "sbx-basic-auth" in plane.secrets
    for name in (
        "sbx-sessions",
        "sbx-runs",
        "sbx-accounts",
        "sbx-workflows",
        "sbx-artifacts",
        "sbx-workspaces",
    ):
        assert name in plane.dicts
    assert plane.image_calls == ["codex"]
    assert plane.image_names["codex"] == "sbx-runtime"
    assert plane.apps["sbx-control"].startswith("https://")
    assert report.base_url == plane.apps["sbx-control"]
    # deployed URL is persisted into the single config source
    assert load(tmp_path / "config.toml", env={}).config.api_base_url == report.base_url
    state = read_deploy_state(env)
    assert state["version"] and state["app_url"] == report.base_url
    assert state["key_fingerprint"] == fingerprint(token)
    assert report.key_created and not report.key_rotated


def test_deploy_is_idempotent(tmp_path) -> None:
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    env = make_env(tmp_path)
    _deploy(tmp_path, plane, env=env)
    first_token = read_key(key_path(env))
    dicts_before = {k: dict(v) for k, v in plane.dicts.items()}
    plane.dicts["sbx-sessions"]["session/abc"] = {"id": "abc"}

    report2, _, _ = _deploy(tmp_path, plane, env=env)
    assert read_key(key_path(env)) == first_token  # key not reminted
    assert plane.secrets["sbx-v1-bootstrap"]["SBX_V1_BOOTSTRAP_KEY"] == first_token
    assert plane.dicts["sbx-sessions"]["session/abc"] == {"id": "abc"}
    assert set(plane.dicts) == set(dicts_before)
    assert not report2.key_created


def test_basic_secret_uses_env_names_control_reads(tmp_path) -> None:
    """The Secret keys must match ``control.config.basic_credentials`` —
    a name the app never reads would silently fall back to sbx/sbx."""
    import os
    import stat

    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    _, env, _ = _deploy(tmp_path, plane)
    secret_env = plane.secrets["sbx-basic-auth"]
    assert set(secret_env) == {"SBX_BASIC_USER", "SBX_BASIC_PASS"}

    from control.config import basic_credentials

    saved = {k: os.environ.get(k) for k in ("SBX_BASIC_USER", "SBX_BASIC_PASS")}
    os.environ.update(secret_env)
    try:
        assert basic_credentials() == (secret_env["SBX_BASIC_USER"], secret_env["SBX_BASIC_PASS"])
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    from sbx.config import basic_auth_path

    assert stat.S_IMODE(os.stat(basic_auth_path(env)).st_mode) == 0o600


def test_legacy_basic_password_env_still_read() -> None:
    """Pre-0.1 docs named the variable SBX_BASIC_PASSWORD; secrets created
    from those docs must not silently degrade to the default password."""
    import os

    from control.config import basic_credentials

    for key in ("SBX_BASIC_USER", "SBX_BASIC_PASS", "SBX_API_PASSWORD"):
        os.environ.pop(key, None)
    os.environ["SBX_BASIC_PASSWORD"] = "legacy-pass"
    try:
        assert basic_credentials() == ("sbx", "legacy-pass")
    finally:
        os.environ.pop("SBX_BASIC_PASSWORD", None)


def test_deploy_missing_codex_secret_is_actionable(tmp_path) -> None:
    plane = FakePlane()
    with pytest.raises(BootstrapError) as exc:
        _deploy(tmp_path, plane)
    assert exc.value.code == "secret_missing"
    assert "modal secret create sbx-codex-auth" in (exc.value.hint or "")


def test_deploy_missing_codex_secret_guides_login_on_clean_home(tmp_path) -> None:
    """When no local credential exists the remediation starts at the
    official login, not a bare secret-create command (SOR-115)."""
    plane = FakePlane()
    with pytest.raises(BootstrapError) as exc:
        _deploy(tmp_path, plane)
    assert "codex login" in (exc.value.hint or "")
    assert "~/.codex/auth.json" in (exc.value.hint or "")
    assert "modal secret create sbx-codex-auth" in (exc.value.hint or "")


def test_deploy_non_codex_providers_skip_codex_secret(tmp_path) -> None:
    """Only selected providers gate onboarding: a devin-only deploy must
    not require ``sbx-codex-auth`` (SOR-115)."""
    plane = FakePlane()  # no sbx-codex-auth — and none needed
    config = BootstrapConfig(providers=("devin",))
    report, _, _ = _deploy(tmp_path, plane, config=config)
    assert plane.image_calls == ["devin"]
    names = [s.name for s in report.steps]
    assert "secret:codex" not in names
    assert plane.deploy_env["SBX_PROVIDERS"] == "devin"


def test_deploy_missing_modal_auth_is_actionable(tmp_path) -> None:
    plane = FakePlane(workspace=None)
    with pytest.raises(BootstrapError) as exc:
        _deploy(tmp_path, plane)
    assert exc.value.code == "modal_auth_missing"
    assert "modal token new" in (exc.value.hint or "")


def test_failed_deploy_is_resumable(tmp_path) -> None:
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    plane.fail_on.add("ensure_image")
    with pytest.raises(BootstrapError):
        _deploy(tmp_path, plane)
    # partial progress is durable — secrets/dicts already exist
    assert "sbx-v1-bootstrap" in plane.secrets
    plane.fail_on.clear()
    report, env, _ = _deploy(tmp_path, plane)
    assert report.base_url
    names = [s.name for s in report.steps]
    assert "image:codex" in names


def test_stale_remote_secret_rotates_with_new_key(tmp_path) -> None:
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    plane.secrets["sbx-v1-bootstrap"] = {"SBX_V1_BOOTSTRAP_KEY": "sbx_oldtoken"}
    report, env, _ = _deploy(tmp_path, plane)
    token = read_key(key_path(env))
    assert plane.secrets["sbx-v1-bootstrap"]["SBX_V1_BOOTSTRAP_KEY"] == token
    assert report.key_rotated


def test_deploy_multi_provider_images(tmp_path) -> None:
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    config = BootstrapConfig(providers=("codex", "devin"))
    _deploy(tmp_path, plane, config=config)
    assert plane.image_calls == ["codex", "devin"]


def test_deploy_materializes_imported_account_secret(tmp_path) -> None:
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    plane.dicts["sbx-accounts"] = {
        "account/devin-1": {
            "id": "devin-1",
            "provider": "devin",
            "secret_name": "sbx-acct-devin-1",
        },
        "credential/devin-1": {
            "provider": "devin",
            "files": {".local/share/devin/credentials.toml": "credential-v1"},
        },
    }
    _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex", "devin")))
    raw = plane.secrets["sbx-acct-devin-1"]["SBX_ACCOUNT_CREDENTIAL"]
    assert json.loads(raw)["provider"] == "devin"
    assert "credential-v1" in raw

    plane.dicts["sbx-accounts"]["credential/devin-1"] = {
        "provider": "devin",
        "files": {".local/share/devin/credentials.toml": "credential-v2"},
    }
    _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex", "devin")))
    raw2 = plane.secrets["sbx-acct-devin-1"]["SBX_ACCOUNT_CREDENTIAL"]
    assert "credential-v2" in raw2 and "credential-v1" not in raw2


def test_deploy_devin_only_does_not_require_codex_secret(tmp_path) -> None:
    """SOR-116 gate: providers=[devin] fresh deploy needs no sbx-codex-auth."""
    plane = FakePlane()  # no secrets at all
    report, _, _ = _deploy(tmp_path, plane, config=BootstrapConfig(providers=("devin",)))
    assert "sbx-codex-auth" not in plane.secrets
    assert plane.image_calls == ["devin"]
    # the Codex preflight never runs — no step, no Secret lookup
    assert all(s.name != "secret:codex" for s in report.steps)
    assert plane.deploy_env["SBX_PROVIDERS"] == "devin"


def test_deploy_mixed_providers_still_require_codex_secret(tmp_path) -> None:
    plane = FakePlane()
    with pytest.raises(BootstrapError) as exc:
        _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex", "devin")))
    assert exc.value.code == "secret_missing"
    assert "modal secret create sbx-codex-auth" in (exc.value.hint or "")
    assert plane.secret_create_calls == 0  # fail-before-write


def test_deploy_empty_providers_fails_before_any_write(tmp_path) -> None:
    plane = FakePlane()
    env = make_env(tmp_path)
    with pytest.raises(BootstrapError) as exc:
        _deploy(tmp_path, plane, env=env, config=BootstrapConfig(providers=()))
    assert exc.value.code == "invalid_providers"
    assert "deploy.providers" in exc.value.message
    assert plane.secret_create_calls == 0
    assert plane.dict_create_calls == 0
    assert plane.deploy_calls == 0
    assert not key_path(env).exists()  # not even the local key was minted


def test_deploy_github_bridge_secret_preflight(tmp_path) -> None:
    """SOR-117: a named GitHub bridge Secret must exist when the gate is
    armed — fail-before-write, never a silently inert remote bridge."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    env = make_env(
        tmp_path,
        {"SBX_GITHUB_EPHEMERAL": "1", "SBX_GITHUB_SECRET_NAME": "sbx-github"},
    )
    with pytest.raises(BootstrapError) as exc:
        _deploy(tmp_path, plane, env=env)
    assert exc.value.code == "secret_missing"
    assert "sbx-github" in str(exc.value)
    assert plane.secret_create_calls == 0  # fail-before-write

    plane.secrets["sbx-github"] = {"GH_TOKEN": "REDACTED_GITHUB"}
    report, _, _ = _deploy(tmp_path, plane, env=env)
    assert any(s.name == "secret:github" for s in report.steps)


def test_deploy_github_bridge_preflight_from_config_file(tmp_path) -> None:
    """SOR-133: gate + Secret name resolve from config.toml (not only env),
    and the resolved pair is replayed into the deploy env for the remote
    app — still without ever touching the token value."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    env = make_env(tmp_path)  # no SBX_GITHUB_* env — file is the source
    config = BootstrapConfig(github_ephemeral=True, github_secret_name="sbx-github")
    with pytest.raises(BootstrapError) as exc:
        _deploy(tmp_path, plane, env=env, config=config)
    assert exc.value.code == "secret_missing"
    assert "sbx-github" in str(exc.value)
    assert plane.secret_create_calls == 0  # fail-before-write

    plane.secrets["sbx-github"] = {"GH_TOKEN": "REDACTED_GITHUB"}
    report, _, _ = _deploy(tmp_path, plane, env=env, config=config)
    assert any(s.name == "secret:github" for s in report.steps)
    assert plane.deploy_env["SBX_GITHUB_EPHEMERAL"] == "1"
    assert plane.deploy_env["SBX_GITHUB_SECRET_NAME"] == "sbx-github"
    assert all("REDACTED_GITHUB" not in v for v in plane.deploy_env.values())


def test_deploy_github_gate_alone_needs_no_secret(tmp_path) -> None:
    """The local-gate path (token in the control-plane env, no named Secret)
    deploys unchanged — the GitHub preflight is opt-in, not ambient."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    env = make_env(tmp_path, {"SBX_GITHUB_EPHEMERAL": "1"})
    report, _, _ = _deploy(tmp_path, plane, env=env)
    assert all(s.name != "secret:github" for s in report.steps)


def test_deploy_unknown_provider_fails_before_any_write(tmp_path) -> None:
    plane = FakePlane()
    with pytest.raises(BootstrapError) as exc:
        _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex", "bogus")))
    assert exc.value.code == "invalid_providers"
    assert "bogus" in exc.value.message
    assert plane.secret_create_calls == 0
    assert plane.deploy_calls == 0


def test_deploy_missing_enabled_account_secret_fails_before_write(tmp_path) -> None:
    """An enabled provider's referenced-but-absent Secret is a real missing
    prerequisite — it must abort before any resource is written."""
    plane = FakePlane()
    plane.dicts["sbx-accounts"] = {
        "account/devin-1": {
            "id": "devin-1",
            "provider": "devin",
            "secret_name": "sbx-acct-devin-1",
        },
        # no credential blob → the materialize step cannot satisfy it
    }
    with pytest.raises(BootstrapError) as exc:
        _deploy(tmp_path, plane, config=BootstrapConfig(providers=("devin",)))
    assert exc.value.code == "account_secret_missing"
    assert "sbx-acct-devin-1" in exc.value.message
    assert plane.secret_create_calls == 0


def test_deploy_ignores_disabled_provider_account_secret(tmp_path) -> None:
    """A devin account's missing Secret is no codex-only prerequisite."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    plane.dicts["sbx-accounts"] = {
        "account/devin-1": {
            "id": "devin-1",
            "provider": "devin",
            "secret_name": "sbx-acct-devin-1",
        },
    }
    report, _, _ = _deploy(tmp_path, plane)  # providers=("codex",)
    assert report.base_url
    assert "sbx-acct-devin-1" not in plane.secrets  # not materialized either


def test_deploy_does_not_overwrite_custom_account_secret(tmp_path) -> None:
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    plane.secrets["customer-managed"] = {"SBX_ACCOUNT_CREDENTIAL": "external"}
    plane.dicts["sbx-accounts"] = {
        "account/grok-1": {
            "id": "grok-1",
            "provider": "grok",
            "secret_name": "customer-managed",
        },
        "credential/grok-1": {
            "provider": "grok",
            "files": {".grok/auth.json": "stored"},
        },
    }
    _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex", "grok")))
    assert plane.secrets["customer-managed"] == {"SBX_ACCOUNT_CREDENTIAL": "external"}
