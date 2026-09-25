"""RC namespace isolation: a parallel deploy binds only its own names.

The release-0.1 RC gate needs a second control app living next to the
production ``sbx-control`` without sharing any durable Dict, Secret, or
named image. These tests pin the contract: every configured name reaches
the deploy subprocess env, image builds publish under the configured names,
and teardown scopes its secret sweep to the configured prefix.
"""

from __future__ import annotations

from sbx_fakes import FakePlane, make_cfg, make_env, make_v1, write_state

from sbx.config import BootstrapConfig
from sbx.deploy import deploy
from sbx.uninstall import uninstall

RC_NAMES = {
    "modal_app_name": "sbx-control-release01-rc",
    "sessions_dict": "rc-sessions",
    "runs_dict": "rc-runs",
    "accounts_dict": "rc-accounts",
    "workflows_dict": "rc-workflows",
    "artifacts_dict": "rc-artifacts",
    "workspaces_dict": "rc-workspaces",
    "account_secret_prefix": "rc-acct-",
    "codex_secret": "rc-codex-auth",
    "basic_secret": "rc-basic-auth",
    "bootstrap_secret": "rc-v1-bootstrap",
    "image_codex": "rc-runtime",
    "image_devin": "rc-runtime-devin",
    "image_antigravity": "rc-runtime-antigravity",
    "image_grok": "rc-runtime-grok",
    "image_opencode": "rc-runtime-opencode",
}

PRODUCTION_NAMES = {
    "sbx-control",
    "sbx-sessions",
    "sbx-runs",
    "sbx-accounts",
    "sbx-workflows",
    "sbx-artifacts",
    "sbx-workspaces",
    "sbx-codex-auth",
    "sbx-basic-auth",
    "sbx-v1-bootstrap",
}


def _rc_config(**overrides) -> BootstrapConfig:
    return BootstrapConfig(**{**RC_NAMES, **overrides})


def test_deploy_env_covers_every_namespace_name() -> None:
    env = _rc_config(providers=("codex",)).deploy_env()
    assert env == {
        "SBX_MODAL_APP_NAME": "sbx-control-release01-rc",
        "SBX_SESSIONS_DICT": "rc-sessions",
        "SBX_RUNS_DICT": "rc-runs",
        "SBX_ACCOUNTS_DICT": "rc-accounts",
        "SBX_WORKFLOWS_DICT": "rc-workflows",
        "SBX_ARTIFACTS_DICT": "rc-artifacts",
        "SBX_WORKSPACES_DICT": "rc-workspaces",
        "SBX_ACCOUNT_SECRET_PREFIX": "rc-acct-",
        "SBX_CODEX_SECRET_NAME": "rc-codex-auth",
        "SBX_BASIC_SECRET_NAME": "rc-basic-auth",
        "SBX_V1_BOOTSTRAP_SECRET_NAME": "rc-v1-bootstrap",
        "SBX_IMAGE_CODEX": "rc-runtime",
        "SBX_IMAGE_DEVIN": "rc-runtime-devin",
        "SBX_IMAGE_ANTIGRAVITY": "rc-runtime-antigravity",
        "SBX_IMAGE_GROK": "rc-runtime-grok",
        "SBX_IMAGE_OPENCODE": "rc-runtime-opencode",
        "SBX_PROVIDERS": "codex",
    }


def test_deploy_uses_only_rc_resources(tmp_path) -> None:
    plane = FakePlane()
    plane.secrets["rc-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    env = make_env(tmp_path)
    cfg = make_cfg(tmp_path, env=env, config=_rc_config(providers=("codex", "devin")))
    transport, _ = make_v1()

    report = deploy(cfg, plane, env=env, transport=transport, sleep=lambda s: None)

    # Every durable Dict + managed Secret is the RC name, none production.
    assert PRODUCTION_NAMES.isdisjoint(plane.dicts)
    assert PRODUCTION_NAMES.isdisjoint(plane.secrets)
    assert set(plane.dicts) == {
        "rc-sessions",
        "rc-runs",
        "rc-accounts",
        "rc-workflows",
        "rc-artifacts",
        "rc-workspaces",
    }
    assert {"rc-codex-auth", "rc-basic-auth", "rc-v1-bootstrap"} <= set(plane.secrets)
    assert plane.image_names == {"codex": "rc-runtime", "devin": "rc-runtime-devin"}
    # The app deploy subprocess received the full namespace env — this is
    # what stops the remote functions from binding production names.
    assert plane.deploy_env == _rc_config(providers=("codex", "devin")).deploy_env()
    assert "sbx-control" not in plane.apps
    assert "sbx-control-release01-rc" in plane.apps
    assert report.base_url == plane.apps["sbx-control-release01-rc"]


def test_purge_credentials_only_sweeps_configured_prefix(tmp_path) -> None:
    env = make_env(tmp_path)
    write_state(tmp_path, {"version": "0.1.0"})
    plane = FakePlane()
    plane.apps["sbx-control-release01-rc"] = "https://ws-test--rc.modal.run"
    for name in (
        "rc-sessions",
        "rc-runs",
        "rc-accounts",
        "rc-workflows",
        "rc-artifacts",
        "rc-workspaces",
    ):
        plane.dicts[name] = {"k": 1}
    plane.secrets.update(
        {
            "rc-codex-auth": {"K": "V"},
            "rc-basic-auth": {"K": "V"},
            "rc-v1-bootstrap": {"K": "V"},
            "rc-acct-devin-1": {"K": "V"},
            # production account Secret must survive an RC teardown
            "sbx-acct-devin-1": {"K": "V"},
        }
    )
    cfg = make_cfg(tmp_path, env=env, config=_rc_config())

    report = uninstall(cfg, plane, env=env, purge_credentials=True)

    assert "sbx-acct-devin-1" in plane.secrets  # production untouched
    assert "rc-acct-devin-1" in report.deleted_secrets
    assert set(plane.secrets) == {"sbx-acct-devin-1"}
