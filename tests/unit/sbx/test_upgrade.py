"""``sbx upgrade``: durable runs/accounts/workflows survive N → N+1."""

from __future__ import annotations

import pytest
from sbx_fakes import FakePlane, make_cfg, make_env, make_v1, write_state

from sbx.deploy import read_deploy_state, snapshot_durable, upgrade
from sbx.errors import BootstrapError


def _deployed(tmp_path, plane, version="0.1.0"):
    """A healthy deployed fixture: secrets + dicts with durable content."""
    env = make_env(tmp_path)
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    for name in (
        "sbx-sessions",
        "sbx-runs",
        "sbx-accounts",
        "sbx-workflows",
        "sbx-artifacts",
        "sbx-workspaces",
    ):
        plane.dicts[name] = {f"key/{name}/1": {"v": 1}}
    write_state(tmp_path, {"version": version, "app_url": "https://old.modal.run"})
    cfg = make_cfg(tmp_path, env=env)
    return cfg, env


def test_upgrade_preserves_durable_stores(tmp_path) -> None:
    plane = FakePlane()
    cfg, env = _deployed(tmp_path, plane)
    before = snapshot_durable(cfg.config, plane)
    transport, _ = make_v1()
    report = upgrade(
        cfg,
        plane,
        env=env,
        transport=transport,
        sleep=lambda s: None,
        version="0.1.1",
    )
    assert report.from_version == "0.1.0"
    assert report.to_version == "0.1.1"
    assert report.durable == before  # every store still readable, same counts
    # the underlying data is untouched, not just the counts
    for name, keys in before.items():
        assert len(plane.dicts[name]) == keys
        assert plane.dicts[name][f"key/{name}/1"] == {"v": 1}
    assert read_deploy_state(env)["version"] == "0.1.1"


def test_upgrade_unreadable_store_aborts_before_deploy(tmp_path) -> None:
    plane = FakePlane()
    cfg, env = _deployed(tmp_path, plane)
    del plane.dicts["sbx-runs"]  # a durable store went missing
    with pytest.raises(BootstrapError) as exc:
        upgrade(cfg, plane, env=env, transport=None, sleep=lambda s: None)
    assert exc.value.code == "durable_unreadable"
    assert plane.deploy_calls == 0  # nothing was touched


def test_upgrade_detects_key_loss(tmp_path, monkeypatch) -> None:
    plane = FakePlane()
    cfg, env = _deployed(tmp_path, plane)
    transport, _ = make_v1()
    original = plane.deploy_app

    def sabotage(app_name: str, *, env=None) -> str:
        plane.dicts["sbx-runs"].pop("key/sbx-runs/1")
        return original(app_name, env=env)

    monkeypatch.setattr(plane, "deploy_app", sabotage)
    with pytest.raises(BootstrapError) as exc:
        upgrade(cfg, plane, env=env, transport=transport, sleep=lambda s: None)
    assert exc.value.code == "durable_lost"
