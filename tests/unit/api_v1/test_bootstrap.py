"""Opt-in production bootstrap for the P2.1 real /v1 gate."""

from __future__ import annotations

from control.api_v1.bootstrap import configure_v1_bootstrap
from control.devin_pool import DevinAccountPool
from fastapi import FastAPI


def test_bootstrap_is_disabled_without_secret_env(monkeypatch) -> None:
    monkeypatch.delenv("SBX_V1_BOOTSTRAP_KEY", raising=False)
    app = FastAPI()
    assert configure_v1_bootstrap(app) is False
    assert not hasattr(app.state, "api_key_store")


def test_bootstrap_seeds_hash_only_key_and_devin_pool(monkeypatch) -> None:
    token = "sbx_" + "a" * 40
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", token)
    monkeypatch.setenv("SBX_DEVIN_ACCOUNT_ID", "devin-gate")
    monkeypatch.setenv("SBX_DEVIN_SECRET_NAME", "sbx-acct-devin-gate")
    monkeypatch.setenv("SBX_DEVIN_NORMAL_SLOTS", "4")
    monkeypatch.setenv("SBX_DEVIN_SOFT_CEILING", "5")
    monkeypatch.setenv("SBX_DEVIN_BURST_SLOTS", "8")
    app = FastAPI()

    assert configure_v1_bootstrap(app) is True
    record = app.state.api_key_store.lookup(token)
    assert record is not None
    assert record.scopes == ("agents", "admin")
    assert token not in repr(record)
    account = app.state.account_registry.get("devin-gate")
    assert account is not None
    assert account.secret_name == "sbx-acct-devin-gate"
    assert account.max_concurrent == 8
    assert isinstance(app.state.scheduler, DevinAccountPool)
    assert app.state.scheduler.normal_slots == 4
    assert app.state.scheduler.soft_ceiling == 5
    assert app.state.scheduler.burst_slots == 8
