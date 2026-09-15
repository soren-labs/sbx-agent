"""SOR-63/D2: ``/v1`` scheduler-wiring contract over a multi-account pool.

``ProviderPool`` — the bootstrap seam composing pinned per-account slot
pools — stands in for D1's persistent scheduler behind the same surface the
routes consume: frozen ``decide``, atomic ``acquire`` → ``SlotLease``, and
``report_failure`` health feedback. These tests pin the API-level contract
— named/auto selection, structured scheduling errors, cooldown/failover,
and terminal run errors marking accounts — for whatever implementation
lands on ``app.state.scheduler``.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from control.api_v1.bootstrap import (
    BootstrapScheduler,
    ProviderPool,
    SingleAccountPool,
)
from control.devin_pool import ScheduleRefused
from tests.unit.api_v1.conftest import create_agent, seed_account, wait_run, wait_sandbox

_AGENT = {"provider": "codex"}


def _post(client: Any, auth: dict[str, str], agent: dict[str, Any] | None = None) -> Any:
    return client.post(
        "/v1/agents",
        json={"prompt": {"text": "hi"}, "agent": dict(agent or _AGENT)},
        headers=auth,
    )


def _multi_codex(
    v1_env,
    accounts: tuple[str, ...] = ("acct-codex-a", "acct-codex-b"),
    *,
    slots: int = 1,
    clock: Any = None,
) -> ProviderPool:
    """Two+ codex accounts behind one ProviderPool on app.state.scheduler."""
    member = {}
    for account_id in accounts:
        seed_account(v1_env, account_id, provider="codex", models=("gpt-5.6-luna",))
        member[account_id] = SingleAccountPool(
            v1_env.registry,
            provider="codex",
            account_id=account_id,
            slots=slots,
            clock=clock,
        )
    pool = ProviderPool(member)
    v1_env.app.state.scheduler = BootstrapScheduler({"codex": pool}, account_pools=member)
    return pool


class _SpyScheduler:
    """Duck-typed scheduler wrapper: same surface, counts report_failure.

    Proves the /v1 seam works through any object exposing decide/acquire/
    report_failure — the shape D1's persistent scheduler will implement.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.reports: list[tuple[str, dict[str, Any]]] = []

    def decide(self, **kwargs: Any) -> Any:
        return self.inner.decide(**kwargs)

    def acquire(self, **kwargs: Any) -> Any:
        return self.inner.acquire(**kwargs)

    def report_failure(self, kind: str, **kwargs: Any) -> Any:
        self.reports.append((kind, kwargs))
        return self.inner.report_failure(kind, **kwargs)


class TestMultiAccountSelection:
    def test_auto_rotates_when_slots_held(self, client, auth, v1_env) -> None:
        pool = _multi_codex(v1_env)
        first = create_agent(client, auth)["agent"]
        second = create_agent(client, auth)["agent"]
        # Per-account slot held → auto fails over to the next account.
        assert first["account_id"] == "acct-codex-a"
        assert second["account_id"] == "acct-codex-b"
        assert pool.active_count == 2
        rec = wait_sandbox(v1_env, second["id"])
        assert rec.sandbox_tags["account_id"] == "acct-codex-b"
        session = json.loads((Path(rec.sandbox_root) / "session.json").read_text())
        assert session["account_id"] == "acct-codex-b"
        client.delete(f"/v1/agents/{first['id']}", headers=auth)
        client.delete(f"/v1/agents/{second['id']}", headers=auth)
        assert pool.active_count == 0

    def test_auto_lru_prefers_never_used_over_freed(self, client, auth, v1_env) -> None:
        pool = _multi_codex(v1_env)
        first = create_agent(client, auth)["agent"]
        assert first["account_id"] == "acct-codex-a"
        client.delete(f"/v1/agents/{first['id']}", headers=auth)
        assert pool.active_count == 0
        # acct-codex-b was never used; LRU picks it over the freed account.
        second = create_agent(client, auth)["agent"]
        assert second["account_id"] == "acct-codex-b"
        client.delete(f"/v1/agents/{second['id']}", headers=auth)

    def test_named_account_pins_selection(self, client, auth, v1_env) -> None:
        _multi_codex(v1_env)
        spec = {"provider": "codex", "account_id": "acct-codex-b"}
        agent = create_agent(client, auth, agent=spec)["agent"]
        assert agent["account_id"] == "acct-codex-b"
        client.delete(f"/v1/agents/{agent['id']}", headers=auth)

    def test_named_busy_409_then_auto_fails_over(self, client, auth, v1_env) -> None:
        _multi_codex(v1_env)
        first = create_agent(client, auth)["agent"]
        assert first["account_id"] == "acct-codex-a"

        busy = _post(client, auth, {"provider": "codex", "account_id": "acct-codex-a"})
        assert busy.status_code == 409
        assert busy.json()["error"]["code"] == "account_busy"

        # auto skips the busy account and lands on the free one.
        second = create_agent(client, auth)["agent"]
        assert second["account_id"] == "acct-codex-b"
        client.delete(f"/v1/agents/{first['id']}", headers=auth)
        client.delete(f"/v1/agents/{second['id']}", headers=auth)

    def test_provider_exhausted_429_shape(self, client, auth, v1_env) -> None:
        pool = _multi_codex(v1_env)
        agents = [create_agent(client, auth)["agent"] for _ in range(2)]
        assert pool.active_count == 2

        refused = _post(client, auth)
        assert refused.status_code == 429
        error = refused.json()["error"]
        assert error["code"] == "provider_exhausted"
        assert error["message"]
        assert error["retry_after"] > 0  # member pool retry hint survives
        for agent in agents:
            client.delete(f"/v1/agents/{agent['id']}", headers=auth)

    def test_named_unavailable_409_variants(self, client, auth, v1_env) -> None:
        _multi_codex(v1_env)
        # Unknown account id.
        missing = _post(client, auth, {"provider": "codex", "account_id": "acct-zzz"})
        assert missing.status_code == 409
        assert missing.json()["error"]["code"] == "account_unavailable"
        # The conftest codex account exists in the registry but is not part
        # of this pool's fleet — the pool is authoritative for its provider.
        outside = _post(client, auth, {"provider": "codex", "account_id": "acct-codex-1"})
        assert outside.status_code == 409
        assert outside.json()["error"]["code"] == "account_unavailable"
        # Unseeded provider: named → 409, auto → 429.
        grok_named = _post(client, auth, {"provider": "grok", "account_id": "grok-1"})
        assert grok_named.status_code == 409
        assert grok_named.json()["error"]["code"] == "account_unavailable"
        grok_auto = _post(client, auth, {"provider": "grok"})
        assert grok_auto.status_code == 429
        assert grok_auto.json()["error"]["code"] == "provider_exhausted"

    def test_invalid_provider_400(self, client, auth) -> None:
        refused = _post(client, auth, {"provider": "bogus"})
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "invalid_provider"

    def test_atomic_acquire_no_oversubscription(self, client, auth, v1_env) -> None:
        """Concurrent creates cannot both take the last free slot."""
        _multi_codex(v1_env, accounts=("acct-codex-a",), slots=1)
        v1_env.app.state.plane.max_concurrent = 8
        results: list[Any] = [None, None]

        def post(i: int) -> None:
            results[i] = _post(client, auth)

        threads = [threading.Thread(target=post, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        codes = sorted(r.status_code for r in results)
        assert codes == [201, 429]
        exhausted = next(r for r in results if r.status_code == 429)
        assert exhausted.json()["error"]["code"] == "provider_exhausted"
        for agent_id in [r.json()["agent"]["id"] for r in results if r.status_code == 201]:
            client.delete(f"/v1/agents/{agent_id}", headers=auth)


class TestCooldownFailover:
    def test_cooling_account_skipped_by_auto(self, client, auth, v1_env) -> None:
        pool = _multi_codex(v1_env)
        scheduler = v1_env.app.state.scheduler
        account = scheduler.report_failure("rate_limited", account_id="acct-codex-a")
        assert account.status == "cooling"
        assert account.cooldown_until is not None
        assert account.last_error == "rate_limited"

        named = _post(client, auth, {"provider": "codex", "account_id": "acct-codex-a"})
        assert named.status_code == 409
        assert named.json()["error"]["code"] == "account_unavailable"

        agent = create_agent(client, auth)["agent"]
        assert agent["account_id"] == "acct-codex-b"
        assert pool.active_count == 1
        client.delete(f"/v1/agents/{agent['id']}", headers=auth)

    def test_all_cooling_exhausts_with_retry_after(self, client, auth, v1_env) -> None:
        _multi_codex(v1_env)
        scheduler = v1_env.app.state.scheduler
        for account_id in ("acct-codex-a", "acct-codex-b"):
            scheduler.report_failure("rate_limited", account_id=account_id, retry_after=45.0)

        refused = _post(client, auth)
        assert refused.status_code == 429
        error = refused.json()["error"]
        assert error["code"] == "provider_exhausted"
        assert 0 < error["retry_after"] <= 45.0

    def test_cooldown_recovers_on_expiry(self, client, auth, v1_env) -> None:
        now = [datetime.now(UTC)]

        def clock() -> datetime:
            return now[0]

        _multi_codex(v1_env, clock=clock)
        scheduler = v1_env.app.state.scheduler
        scheduler.report_failure("rate_limited", account_id="acct-codex-a", retry_after=30.0)

        agent = create_agent(client, auth)["agent"]
        assert agent["account_id"] == "acct-codex-b"
        client.delete(f"/v1/agents/{agent['id']}", headers=auth)

        # Cooldown elapsed → the account is pickable again.
        now[0] = now[0] + timedelta(seconds=31)
        recovered = create_agent(client, auth)["agent"]
        assert recovered["account_id"] == "acct-codex-a"
        assert v1_env.registry.get("acct-codex-a").status == "active"
        client.delete(f"/v1/agents/{recovered['id']}", headers=auth)

    def test_auth_invalid_marks_invalid_and_fails_over(self, client, auth, v1_env) -> None:
        _multi_codex(v1_env)
        scheduler = v1_env.app.state.scheduler
        account = scheduler.report_failure("auth_invalid", account_id="acct-codex-a")
        assert account.status == "invalid"
        assert account.cooldown_until is None  # permanent: no auto-recovery

        named = _post(client, auth, {"provider": "codex", "account_id": "acct-codex-a"})
        assert named.status_code == 409
        agent = create_agent(client, auth)["agent"]
        assert agent["account_id"] == "acct-codex-b"
        client.delete(f"/v1/agents/{agent['id']}", headers=auth)


class TestRunFailureFeedback:
    """Terminal provider run-errors feed account health through the API."""

    def test_auth_invalid_run_marks_account_and_fails_over(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        _multi_codex(v1_env)
        spy = _SpyScheduler(v1_env.app.state.scheduler)
        v1_env.app.state.scheduler = spy

        # Burn last_used_at on acct-codex-b so acct-codex-a is the LRU pick:
        # the failover assertion below then proves exclusion, not just LRU.
        first = create_agent(client, auth)["agent"]
        client.delete(f"/v1/agents/{first['id']}", headers=auth)
        assert first["account_id"] == "acct-codex-a"
        second = create_agent(client, auth)["agent"]
        client.delete(f"/v1/agents/{second['id']}", headers=auth)
        assert second["account_id"] == "acct-codex-b"

        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "auth_invalid")
        bad = create_agent(client, auth, agent={"provider": "codex", "account_id": "acct-codex-a"})[
            "agent"
        ]
        run = wait_run(client, auth, bad["id"], "run-1")
        assert run["status"] == "ERROR"
        assert run["error"]["code"] == "auth_invalid"
        assert run["account_id"] == "acct-codex-a"
        assert v1_env.registry.get("acct-codex-a").status == "invalid"
        assert [kind for kind, _ in spy.reports] == ["auth_invalid"]

        # Rendering the same terminal run again does not re-report.
        client.get(f"/v1/agents/{bad['id']}/runs/run-1", headers=auth)
        client.get(f"/v1/agents/{bad['id']}/runs", headers=auth)
        assert len(spy.reports) == 1

        # auto now excludes the invalid account even though it is the LRU.
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
        healthy = create_agent(client, auth)["agent"]
        assert healthy["account_id"] == "acct-codex-b"
        assert wait_run(client, auth, healthy["id"], "run-1")["status"] == "FINISHED"
        client.delete(f"/v1/agents/{bad['id']}", headers=auth)
        client.delete(f"/v1/agents/{healthy['id']}", headers=auth)

    def test_runtime_error_does_not_mark_account(self, client, auth, v1_env, monkeypatch) -> None:
        _multi_codex(v1_env)
        spy = _SpyScheduler(v1_env.app.state.scheduler)
        v1_env.app.state.scheduler = spy

        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "nonzero")
        agent = create_agent(
            client, auth, agent={"provider": "codex", "account_id": "acct-codex-a"}
        )["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "ERROR"
        # A non-health runtime failure never touches account status.
        assert v1_env.registry.get("acct-codex-a").status == "active"
        assert spy.reports == []
        client.delete(f"/v1/agents/{agent['id']}", headers=auth)

    def test_decide_only_scheduler_reports_nothing(self, client, auth, v1_env, monkeypatch) -> None:
        """The frozen decide-only Scheduler has no report_failure: no-op."""
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "auth_invalid")
        agent = create_agent(client, auth)["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "ERROR"
        assert run["error"]["code"] == "auth_invalid"
        # InMemoryScheduler accounts stay active — nothing listened.
        assert v1_env.registry.get("acct-codex-1").status == "active"
        client.delete(f"/v1/agents/{agent['id']}", headers=auth)


class TestProviderPoolUnit:
    """Pool-shape contract the /v1 seam relies on (no HTTP)."""

    def test_empty_and_mixed_provider_rejected(self, v1_env) -> None:
        with pytest.raises(ValueError):
            ProviderPool({})
        seed_account(v1_env, "acct-g", provider="grok")
        other = SingleAccountPool(v1_env.registry, provider="grok", account_id="acct-g", slots=1)
        seed_account(v1_env, "acct-c", provider="codex")
        member = SingleAccountPool(v1_env.registry, provider="codex", account_id="acct-c", slots=1)
        with pytest.raises(ValueError):
            ProviderPool({"acct-g": other, "acct-c": member})

    def test_decide_mirrors_acquire_pick(self, v1_env) -> None:
        _multi_codex(v1_env)
        scheduler = v1_env.app.state.scheduler
        assert scheduler.decide(provider="codex").account.id == "acct-codex-a"
        lease = scheduler.acquire(provider="codex")
        assert lease.account.id == "acct-codex-a"
        assert scheduler.decide(provider="codex").account.id == "acct-codex-b"
        lease.release()
        # LRU: never-used acct-codex-b still beats the freed acct-codex-a.
        assert scheduler.decide(provider="codex").account.id == "acct-codex-b"

    def test_provider_mismatch_and_bad_account(self, v1_env) -> None:
        pool = _multi_codex(v1_env)
        assert pool.decide(provider="grok").error == "invalid_provider"
        with pytest.raises(ScheduleRefused) as excinfo:
            pool.acquire(provider="grok")
        assert excinfo.value.error == "invalid_provider"
        with pytest.raises(ScheduleRefused) as excinfo:
            pool.acquire(provider="codex", account="acct-zzz")
        assert excinfo.value.error == "account_unavailable"
