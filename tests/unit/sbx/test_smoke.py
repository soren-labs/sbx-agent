"""``sbx smoke``: minimal /v1 agent → terminal → cleanup (SOR-98)."""

from __future__ import annotations

import pytest
from sbx.config import BootstrapConfig, key_path
from sbx.errors import BootstrapError
from sbx.keys import load_or_create_key
from sbx.smoke import run_smoke
from sbx_fakes import make_cfg, make_env, make_v1


def _cfg(tmp_path, token=None):
    env = make_env(tmp_path)
    if token:
        load_or_create_key(key_path(env))  # creates the state dir + file
        key_path(env).write_text(token + "\n")
    config = BootstrapConfig(api_base_url="https://ws--sbx-control-fastapi-app.modal.run")
    cfg = make_cfg(tmp_path, env=env, config=config)
    return cfg, env


def test_smoke_finished_and_cleans_up(tmp_path) -> None:
    token = "sbx_smoketoken"
    cfg, env = _cfg(tmp_path, token=token)
    transport, http = make_v1(token=token, run_statuses=["RUNNING", "FINISHED"])
    result = run_smoke(cfg, env=env, transport=transport, sleep=lambda s: None)
    assert result.status == "FINISHED"
    assert result.agent_id in http["deleted"]  # agent always cleaned up


def test_smoke_error_run_fails_and_cleans_up(tmp_path) -> None:
    token = "sbx_smoketoken"
    cfg, env = _cfg(tmp_path, token=token)
    transport, http = make_v1(token=token, run_statuses=["RUNNING", "ERROR"])
    with pytest.raises(BootstrapError) as exc:
        run_smoke(cfg, env=env, transport=transport, sleep=lambda s: None)
    assert exc.value.code == "smoke_run_failed"
    assert http["deleted"]  # cleanup still ran


def test_smoke_timeout(tmp_path) -> None:
    token = "sbx_smoketoken"
    cfg, env = _cfg(tmp_path, token=token)
    transport, http = make_v1(token=token, run_statuses=["RUNNING"] * 50)
    clock = {"t": 0.0}

    def monotonic() -> float:
        return clock["t"]

    def sleep(s: float) -> None:
        clock["t"] += s

    with pytest.raises(BootstrapError) as exc:
        run_smoke(
            cfg,
            env=env,
            transport=transport,
            timeout_s=10.0,
            poll_s=5.0,
            sleep=sleep,
            monotonic=monotonic,
        )
    assert exc.value.code == "smoke_timeout"
    assert http["deleted"]


def test_smoke_no_key_is_actionable(tmp_path) -> None:
    cfg, env = _cfg(tmp_path)  # no key file
    with pytest.raises(BootstrapError) as exc:
        run_smoke(cfg, env=env, transport=None)
    assert exc.value.code == "api_key_missing"
    assert "sbx deploy" in (exc.value.hint or "")


def test_smoke_no_base_url_is_actionable(tmp_path) -> None:
    env = make_env(tmp_path)
    cfg = make_cfg(tmp_path, env=env, config=BootstrapConfig())
    with pytest.raises(BootstrapError) as exc:
        run_smoke(cfg, env=env, transport=None)
    assert exc.value.code == "config_missing"


def test_smoke_no_providers_is_actionable(tmp_path) -> None:
    env = make_env(tmp_path)
    load_or_create_key(key_path(env))
    cfg = make_cfg(
        tmp_path,
        env=env,
        config=BootstrapConfig(
            providers=(), api_base_url="https://ws--sbx-control-fastapi-app.modal.run"
        ),
    )
    with pytest.raises(BootstrapError) as exc:
        run_smoke(cfg, env=env, transport=None)
    assert exc.value.code == "config_missing"


def test_smoke_provider_exhausted_is_actionable(tmp_path) -> None:
    token = "sbx_smoketoken"
    cfg, env = _cfg(tmp_path, token=token)
    transport, _ = make_v1(
        token=token,
        create_error=(429, "provider_exhausted", "no account has a free slot"),
    )
    with pytest.raises(BootstrapError) as exc:
        run_smoke(cfg, env=env, transport=transport)
    assert exc.value.code == "smoke_create_failed"
    assert "provider_exhausted" in (exc.value.hint or "")
