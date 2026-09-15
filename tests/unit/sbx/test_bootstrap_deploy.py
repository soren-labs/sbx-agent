"""``sbx deploy``: idempotent pipeline, secrets wired, actionable failures."""

from __future__ import annotations

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
    for name in ("sbx-sessions", "sbx-runs", "sbx-accounts", "sbx-workflows"):
        assert name in plane.dicts
    assert plane.image_calls == ["codex"]
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
