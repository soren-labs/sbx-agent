"""``sbx uninstall``: scoped cleanup; credentials preserved by default."""

from __future__ import annotations

import pytest
from sbx_fakes import FakePlane, make_cfg, make_env, write_state

from sbx.config import key_path
from sbx.errors import BootstrapError
from sbx.keys import load_or_create_key
from sbx.plane import SandboxInfo
from sbx.uninstall import uninstall


def _deployed(tmp_path):
    env = make_env(tmp_path)
    load_or_create_key(key_path(env))
    write_state(tmp_path, {"version": "0.1.0"})
    plane = FakePlane()
    plane.apps["sbx-control"] = "https://ws-test--sbx-control-fastapi-app.modal.run"
    for name in (
        "sbx-sessions",
        "sbx-runs",
        "sbx-accounts",
        "sbx-workflows",
        "sbx-artifacts",
        "sbx-workspaces",
    ):
        plane.dicts[name] = {"k": 1}
    for name in ("sbx-codex-auth", "sbx-basic-auth", "sbx-v1-bootstrap", "sbx-acct-devin-1"):
        plane.secrets[name] = {"K": "V"}
    plane.sandboxes["sb_1"] = SandboxInfo(id="sb_1", tags={"session_id": "s1"})
    plane.sandboxes["sb_2"] = SandboxInfo(id="sb_2", tags={"session_id": "s2"})
    cfg = make_cfg(tmp_path, env=env)
    return cfg, plane, env


def test_uninstall_default_scope_preserves_credentials(tmp_path) -> None:
    cfg, plane, env = _deployed(tmp_path)
    report = uninstall(cfg, plane, env=env)
    # all sbx sandboxes terminated; Sandbox.list shows no leftover
    assert set(report.terminated_sandboxes) == {"sb_1", "sb_2"}
    assert plane.list_sandboxes("sbx-control") == []
    assert report.app_stopped
    # durable data + user credentials preserved by default
    assert set(plane.dicts) == {
        "sbx-sessions",
        "sbx-runs",
        "sbx-accounts",
        "sbx-workflows",
        "sbx-artifacts",
        "sbx-workspaces",
    }
    assert set(plane.secrets) == {
        "sbx-codex-auth",
        "sbx-basic-auth",
        "sbx-v1-bootstrap",
        "sbx-acct-devin-1",
    }
    assert key_path(env).is_file()  # local key file kept
    assert "sbx-codex-auth" in report.preserved


def test_uninstall_purge_data_removes_dicts(tmp_path) -> None:
    cfg, plane, env = _deployed(tmp_path)
    report = uninstall(cfg, plane, env=env, purge_data=True)
    assert plane.dicts == {}
    assert set(report.deleted_dicts) == {
        "sbx-sessions",
        "sbx-runs",
        "sbx-accounts",
        "sbx-workflows",
        "sbx-artifacts",
        "sbx-workspaces",
    }
    assert set(plane.secrets)  # credentials still preserved
    assert key_path(env).is_file()


def test_uninstall_purge_credentials_removes_secrets_and_local_key(tmp_path) -> None:
    cfg, plane, env = _deployed(tmp_path)
    report = uninstall(cfg, plane, env=env, purge_credentials=True)
    assert plane.secrets == {}  # managed + sbx-acct-* all gone
    assert "sbx-acct-devin-1" in report.deleted_secrets
    assert not key_path(env).exists()
    assert not (tmp_path / "state" / "deploy.json").exists()
    assert plane.dicts  # durable data preserved unless --purge-data


def test_uninstall_leftover_sandbox_fails(tmp_path) -> None:
    cfg, plane, env = _deployed(tmp_path)
    plane.terminate_noop = True  # terminate calls succeed but sandboxes persist
    with pytest.raises(BootstrapError) as exc:
        uninstall(cfg, plane, env=env)
    assert exc.value.code == "uninstall_leftover"


def test_uninstall_fails_loudly_when_sandbox_list_breaks(tmp_path) -> None:
    """A plane that cannot enumerate sandboxes must not report a clean teardown."""
    cfg, plane, env = _deployed(tmp_path)
    plane.fail_on.add("list_sandboxes")
    with pytest.raises(BootstrapError) as exc:
        uninstall(cfg, plane, env=env)
    assert exc.value.code == "fake_fail"
    assert plane.sandboxes  # nothing claimed terminated


def test_uninstall_nothing_deployed_is_clean(tmp_path) -> None:
    env = make_env(tmp_path)
    cfg = make_cfg(tmp_path, env=env)
    plane = FakePlane()
    report = uninstall(cfg, plane, env=env)
    assert report.terminated_sandboxes == ()
    assert report.app_stopped is False
