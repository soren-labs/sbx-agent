"""Opt-in production bootstrap for the P2 real /v1 gate."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from control.api_v1.bootstrap import (
    BootstrapScheduler,
    ProviderPool,
    SingleAccountPool,
    configure_v1_bootstrap,
)
from control.app import create_app
from control.backend import LocalProcessBackend
from control.devin_pool import DevinAccountPool, ScheduleRefused
from control.store import InMemoryStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

P2_CORE_PROVIDERS = ("codex", "devin", "antigravity", "grok")


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
    scheduler = app.state.scheduler
    assert isinstance(scheduler, BootstrapScheduler)
    devin = scheduler.pools["devin"]
    assert isinstance(devin, DevinAccountPool)
    assert devin.normal_slots == 4
    assert devin.soft_ceiling == 5
    assert devin.burst_slots == 8


def test_bootstrap_allows_explicit_ephemeral_secret(monkeypatch) -> None:
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", "sbx_" + "b" * 40)
    monkeypatch.setenv("SBX_DEVIN_ACCOUNT_ID", "devin-gate")
    monkeypatch.setenv("SBX_DEVIN_SECRET_NAME", "")
    app = FastAPI()
    assert configure_v1_bootstrap(app) is True
    account = app.state.account_registry.get("devin-gate")
    assert account is not None
    assert account.secret_name == ""


def test_bootstrap_seeds_all_four_providers(monkeypatch) -> None:
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", "sbx_" + "c" * 40)
    app = FastAPI()
    assert configure_v1_bootstrap(app) is True

    by_provider = {a.provider: a for a in app.state.account_registry.list()}
    assert set(by_provider) == set(P2_CORE_PROVIDERS)
    assert by_provider["codex"].id == "codex-1"
    # Codex keeps the default CODEX_AUTH_JSON credential path (no named Secret).
    assert by_provider["codex"].secret_name == ""
    assert by_provider["devin"].secret_name == "sbx-acct-devin-1"
    assert by_provider["antigravity"].secret_name == "sbx-acct-antigravity-1"
    assert by_provider["grok"].secret_name == "sbx-acct-grok-1"
    assert all(a.status == "active" for a in by_provider.values())
    assert set(app.state.scheduler.pools) == set(P2_CORE_PROVIDERS)


def test_scheduler_decides_each_provider(monkeypatch) -> None:
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", "sbx_" + "d" * 40)
    app = FastAPI()
    configure_v1_bootstrap(app)
    scheduler = app.state.scheduler

    for provider in P2_CORE_PROVIDERS:
        decision = scheduler.decide(provider=provider, account="auto")
        assert decision.error is None
        assert decision.account is not None
        assert decision.account.provider == provider
        named = scheduler.decide(provider=provider, account=decision.account.id)
        assert named.error is None
        assert named.account is not None
        assert named.account.id == decision.account.id


def test_scheduler_acquire_enforces_flat_slot_cap(monkeypatch) -> None:
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", "sbx_" + "e" * 40)
    monkeypatch.setenv("SBX_GROK_SLOTS", "2")
    app = FastAPI()
    configure_v1_bootstrap(app)
    scheduler = app.state.scheduler

    leases = [scheduler.acquire(provider="grok") for _ in range(2)]
    assert [lease.account.provider for lease in leases] == ["grok", "grok"]
    with pytest.raises(ScheduleRefused) as excinfo:
        scheduler.acquire(provider="grok")
    assert excinfo.value.error == "provider_exhausted"
    leases[0].release()
    scheduler.acquire(provider="grok").release()
    for lease in leases[1:]:
        lease.release()


def test_scheduler_unseeded_and_unknown_providers(monkeypatch) -> None:
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", "sbx_" + "f" * 40)
    app = FastAPI()
    configure_v1_bootstrap(app)
    scheduler = app.state.scheduler

    # opencode is contract-valid but deferred: exhausted, not invalid.
    exhausted = scheduler.decide(provider="opencode", account="auto")
    assert exhausted.error == "provider_exhausted"
    assert exhausted.retry_after is not None and exhausted.retry_after > 0
    with pytest.raises(ScheduleRefused) as excinfo:
        scheduler.acquire(provider="opencode")
    assert excinfo.value.error == "provider_exhausted"
    named = scheduler.decide(provider="opencode", account="opencode-1")
    assert named.error == "account_unavailable"
    with pytest.raises(ScheduleRefused) as excinfo:
        scheduler.acquire(provider="opencode", account="opencode-1")
    assert excinfo.value.error == "account_unavailable"

    assert scheduler.decide(provider="bogus", account="auto").error == "invalid_provider"
    with pytest.raises(ScheduleRefused) as excinfo:
        scheduler.acquire(provider="bogus")
    assert excinfo.value.error == "invalid_provider"


def test_provider_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", "sbx_" + "0" * 40)
    monkeypatch.setenv("SBX_GROK_ACCOUNT_ID", "grok-gate")
    monkeypatch.setenv("SBX_ANTIGRAVITY_SECRET_NAME", "sbx-acct-custom-agy")
    monkeypatch.setenv("SBX_ANTIGRAVITY_SLOTS", "6")
    monkeypatch.setenv("SBX_CODEX_SECRET_NAME", "sbx-acct-codex-9")
    app = FastAPI()
    configure_v1_bootstrap(app)

    registry = app.state.account_registry
    assert registry.get("grok-gate") is not None
    agy = registry.get("antigravity-1")
    assert agy is not None
    assert agy.secret_name == "sbx-acct-custom-agy"
    assert agy.max_concurrent == 6
    codex = registry.get("codex-1")
    assert codex is not None
    assert codex.secret_name == "sbx-acct-codex-9"


def test_v1_agents_schedule_all_four_providers(monkeypatch, stub_runner) -> None:
    """End-to-end through the product API: each provider creates an agent,
    holds its slot lease, and releases on delete."""
    token = "sbx_" + "1" * 40
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", token)
    backend = LocalProcessBackend()
    store = InMemoryStore()
    app = create_app(
        backend=backend,
        store=store,
        runner_cmd=[sys.executable, str(stub_runner)],
        max_concurrent=16,
    )
    auth = {"Authorization": f"Bearer {token}"}
    try:
        with TestClient(app) as client:
            scheduler = app.state.scheduler
            for provider in P2_CORE_PROVIDERS:
                resp = client.post(
                    "/v1/agents",
                    json={"prompt": {"text": "ping"}, "agent": {"provider": provider}},
                    headers=auth,
                )
                assert resp.status_code == 201, resp.text
                agent = resp.json()["agent"]
                assert agent["provider"] == provider
                assert agent["account_id"] == f"{provider}-1"
                assert scheduler.pools[provider].active_count == 1

                # SOR-82 A2: provisioning is async — wait for runner init.
                deadline = time.monotonic() + 15
                rec = None
                while time.monotonic() < deadline:
                    rec = store.get(agent["id"])
                    if rec is not None and rec.status != "creating":
                        break
                    time.sleep(0.05)
                assert rec is not None and rec.sandbox_root is not None
                assert rec.sandbox_tags["provider"] == provider
                session = json.loads((Path(rec.sandbox_root) / "session.json").read_text())
                assert session["provider"] == provider
                assert session["account_id"] == f"{provider}-1"

                deleted = client.delete(f"/v1/agents/{agent['id']}", headers=auth)
                assert deleted.status_code == 200
                assert scheduler.pools[provider].active_count == 0

            refused = client.post(
                "/v1/agents",
                json={"prompt": {"text": "hi"}, "agent": {"provider": "opencode"}},
                headers=auth,
            )
            assert refused.status_code == 429
            assert refused.json()["error"]["code"] == "provider_exhausted"
    finally:
        for handle in list(backend.list()):
            backend.terminate(handle)


def test_v1_named_account_and_slot_cap(monkeypatch, stub_runner) -> None:
    token = "sbx_" + "2" * 40
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", token)
    monkeypatch.setenv("SBX_ANTIGRAVITY_SLOTS", "1")
    backend = LocalProcessBackend()
    app = create_app(
        backend=backend,
        store=InMemoryStore(),
        runner_cmd=[sys.executable, str(stub_runner)],
        max_concurrent=16,
    )
    auth = {"Authorization": f"Bearer {token}"}
    try:
        with TestClient(app) as client:
            body = {
                "prompt": {"text": "ping"},
                "agent": {"provider": "antigravity", "account_id": "antigravity-1"},
            }
            resp = client.post("/v1/agents", json=body, headers=auth)
            assert resp.status_code == 201, resp.text
            agent_id = resp.json()["agent"]["id"]
            busy = client.post("/v1/agents", json=body, headers=auth)
            assert busy.status_code == 409
            assert busy.json()["error"]["code"] == "account_busy"
            exhausted = client.post(
                "/v1/agents",
                json={"prompt": {"text": "hi"}, "agent": {"provider": "antigravity"}},
                headers=auth,
            )
            assert exhausted.status_code == 429
            assert exhausted.json()["error"]["code"] == "provider_exhausted"
            unknown = client.post(
                "/v1/agents",
                json={
                    "prompt": {"text": "hi"},
                    "agent": {"provider": "grok", "account_id": "grok-missing"},
                },
                headers=auth,
            )
            assert unknown.status_code == 409
            assert unknown.json()["error"]["code"] == "account_unavailable"
            assert client.delete(f"/v1/agents/{agent_id}", headers=auth).status_code == 200
    finally:
        for handle in list(backend.list()):
            backend.terminate(handle)


def test_bootstrap_multi_account_json_seeds_provider_pool(monkeypatch) -> None:
    """``SBX_<PROVIDER>_ACCOUNTS`` JSON → registry + ProviderPool (SOR-63/D2)."""
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", "sbx_" + "3" * 40)
    monkeypatch.setenv(
        "SBX_ANTIGRAVITY_ACCOUNTS",
        json.dumps(
            [
                {"id": "agy-a", "slots": 1, "models": ["gemini-3.8-flash-low"]},
                {"id": "agy-b", "slots": 1, "label": "AGY second"},
                {"id": "agy-c", "slots": 1, "secret_name": "sbx-acct-agy-c"},
            ]
        ),
    )
    app = FastAPI()
    assert configure_v1_bootstrap(app) is True

    registry = app.state.account_registry
    seeded = [a.id for a in registry.list("antigravity")]
    assert seeded == ["agy-a", "agy-b", "agy-c"]
    assert registry.get("agy-a").max_concurrent == 1
    assert registry.get("agy-a").models == ("gemini-3.8-flash-low",)
    assert registry.get("agy-b").label == "AGY second"
    assert registry.get("agy-b").secret_name == "sbx-acct-agy-b"
    assert registry.get("agy-c").secret_name == "sbx-acct-agy-c"
    # Other providers keep their single-account pools.
    assert isinstance(app.state.scheduler.pools["codex"], SingleAccountPool)

    agy = app.state.scheduler.pools["antigravity"]
    assert isinstance(agy, ProviderPool)
    # auto rotates across members as slots fill; exhaustion is structured.
    leases = [agy.acquire(provider="antigravity") for _ in range(3)]
    assert {lease.account.id for lease in leases} == {"agy-a", "agy-b", "agy-c"}
    with pytest.raises(ScheduleRefused) as excinfo:
        agy.acquire(provider="antigravity")
    assert excinfo.value.error == "provider_exhausted"
    for lease in leases:
        lease.release()
    assert agy.active_count == 0


def test_bootstrap_multi_account_malformed_json_fails(monkeypatch) -> None:
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", "sbx_" + "4" * 40)
    monkeypatch.setenv("SBX_GROK_ACCOUNTS", "not json")
    with pytest.raises(ValueError, match="SBX_GROK_ACCOUNTS"):
        configure_v1_bootstrap(FastAPI())
    monkeypatch.setenv("SBX_GROK_ACCOUNTS", "[]")
    with pytest.raises(ValueError, match="non-empty"):
        configure_v1_bootstrap(FastAPI())
    monkeypatch.setenv("SBX_GROK_ACCOUNTS", json.dumps([{"slots": 1}]))
    with pytest.raises(ValueError, match="non-empty 'id'"):
        configure_v1_bootstrap(FastAPI())


def test_bootstrap_scheduler_report_failure_routes_to_account(monkeypatch) -> None:
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", "sbx_" + "5" * 40)
    monkeypatch.setenv("SBX_GROK_ACCOUNTS", json.dumps([{"id": "grok-a"}, {"id": "grok-b"}]))
    app = FastAPI()
    configure_v1_bootstrap(app)
    scheduler = app.state.scheduler

    account = scheduler.report_failure("rate_limited", account_id="grok-a", retry_after=30.0)
    assert account.id == "grok-a"
    assert account.status == "cooling"
    # auto skips the cooling member; grok-b still serves.
    lease = scheduler.acquire(provider="grok")
    assert lease.account.id == "grok-b"
    lease.release()
    # Unknown account → KeyError (the reporter treats it as a no-op).
    with pytest.raises(KeyError):
        scheduler.report_failure("rate_limited", account_id="grok-zzz")


def test_v1_multi_account_grok_gate(monkeypatch, stub_runner) -> None:
    """End-to-end: a 2-account Grok pool serves auto creates across members
    and refuses structurally once both slots are held (SOR-63/D2 seam)."""
    token = "sbx_" + "6" * 40
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", token)
    monkeypatch.setenv(
        "SBX_GROK_ACCOUNTS",
        json.dumps([{"id": "grok-a", "slots": 1}, {"id": "grok-b", "slots": 1}]),
    )
    backend = LocalProcessBackend()
    app = create_app(
        backend=backend,
        store=InMemoryStore(),
        runner_cmd=[sys.executable, str(stub_runner)],
        max_concurrent=16,
    )
    auth = {"Authorization": f"Bearer {token}"}
    try:
        with TestClient(app) as client:
            picked = set()
            agents = []
            for _ in range(2):
                resp = client.post(
                    "/v1/agents",
                    json={"prompt": {"text": "ping"}, "agent": {"provider": "grok"}},
                    headers=auth,
                )
                assert resp.status_code == 201, resp.text
                agent = resp.json()["agent"]
                assert agent["provider"] == "grok"
                picked.add(agent["account_id"])
                agents.append(agent["id"])
            assert picked == {"grok-a", "grok-b"}

            exhausted = client.post(
                "/v1/agents",
                json={"prompt": {"text": "again"}, "agent": {"provider": "grok"}},
                headers=auth,
            )
            assert exhausted.status_code == 429
            assert exhausted.json()["error"]["code"] == "provider_exhausted"

            busy = client.post(
                "/v1/agents",
                json={
                    "prompt": {"text": "named"},
                    "agent": {"provider": "grok", "account_id": "grok-a"},
                },
                headers=auth,
            )
            assert busy.status_code == 409
            assert busy.json()["error"]["code"] == "account_busy"

            for agent_id in agents:
                assert client.delete(f"/v1/agents/{agent_id}", headers=auth).status_code == 200
            assert app.state.scheduler.pools["grok"].active_count == 0
    finally:
        for handle in list(backend.list()):
            backend.terminate(handle)
