"""SOR-75: minimal single-Devin-account pool/scheduler (P2.1-S2)."""

from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from control.devin_pool import (
    DEFAULT_BURST_SLOTS,
    DEFAULT_NORMAL_SLOTS,
    DEFAULT_SOFT_CEILING,
    DevinAccountPool,
    ScheduleRefused,
    SlotLease,
)
from control.ports import Account, Scheduler
from tests.fakes.fake_ports import InMemoryAccountRegistry


@pytest.fixture(autouse=True)
def _clean_devin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("SBX_DEVIN_"):
            monkeypatch.delenv(key, raising=False)


def _account(id: str = "devin-1", provider: str = "devin", **kw: Any) -> Account:
    return Account(id=id, provider=provider, label=id, **kw)


def _registry(*accounts: Account) -> InMemoryAccountRegistry:
    reg = InMemoryAccountRegistry()
    for acct in accounts or (_account(),):
        reg.put(acct)
    return reg


def _pool(reg: InMemoryAccountRegistry | None = None, **kw: Any) -> DevinAccountPool:
    return DevinAccountPool(reg if reg is not None else _registry(), **kw)


class TestDecide:
    def test_conforms_to_scheduler_protocol(self) -> None:
        assert isinstance(_pool(), Scheduler)

    def test_auto_resolves_single_account(self) -> None:
        pool = _pool()
        decision = pool.decide(provider="devin")
        assert decision.account is not None
        assert decision.account.id == "devin-1"
        assert decision.error is None
        assert pool.decide(provider="devin", account=None).account is not None

    def test_named_account_resolves(self) -> None:
        decision = _pool().decide(provider="devin", account="devin-1")
        assert decision.account is not None
        assert decision.account.id == "devin-1"

    def test_other_providers_invalid(self) -> None:
        pool = _pool()
        for provider in ("codex", "antigravity", "grok", "opencode", "nope"):
            assert pool.decide(provider=provider).error == "invalid_provider"

    def test_unknown_or_foreign_named_account(self) -> None:
        reg = _registry(_account("devin-1"), _account("codex-1", provider="codex"))
        pool = _pool(reg)
        assert pool.decide(provider="devin", account="missing").error == "account_unavailable"
        assert pool.decide(provider="devin", account="codex-1").error == "account_unavailable"

    def test_no_devin_account_is_exhausted(self) -> None:
        pool = DevinAccountPool(InMemoryAccountRegistry())
        decision = pool.decide(provider="devin")
        assert decision.error == "provider_exhausted"
        assert decision.retry_after is not None
        with pytest.raises(ScheduleRefused) as exc:
            pool.acquire()
        assert exc.value.error == "provider_exhausted"

    def test_decide_is_consultative(self) -> None:
        pool = _pool()
        for _ in range(DEFAULT_BURST_SLOTS + 4):
            assert pool.decide(provider="devin").account is not None
        assert pool.active_count == 0

    def test_decide_reflects_full_pool(self) -> None:
        pool = _pool()
        leases = [pool.acquire() for _ in range(DEFAULT_BURST_SLOTS)]
        auto = pool.decide(provider="devin")
        assert auto.error == "provider_exhausted"
        assert auto.retry_after is not None
        assert pool.decide(provider="devin", account="devin-1").error == "account_busy"
        for lease in leases:
            lease.release()


class TestSlots:
    def test_four_normal_slots(self) -> None:
        pool = _pool()
        leases = [pool.acquire() for _ in range(DEFAULT_NORMAL_SLOTS)]
        assert pool.active_count == DEFAULT_NORMAL_SLOTS
        assert [lease.slot for lease in leases] == list(range(1, 5))
        assert all(not lease.soft and not lease.burst for lease in leases)
        assert all(lease.account.id == "devin-1" for lease in leases)
        for lease in leases:
            lease.release()
        assert pool.active_count == 0

    def test_soft_fifth_then_burst_six_to_eight(self) -> None:
        pool = _pool()
        leases = [pool.acquire() for _ in range(DEFAULT_BURST_SLOTS)]
        assert [lease.soft for lease in leases] == [False] * 4 + [True] + [False] * 3
        assert [lease.burst for lease in leases] == [False] * 5 + [True] * 3
        assert [lease.slot for lease in leases] == list(range(1, 9))

    def test_ninth_rejected_and_frees_up(self) -> None:
        pool = _pool()
        leases = [pool.acquire() for _ in range(DEFAULT_BURST_SLOTS)]
        with pytest.raises(ScheduleRefused) as exc:
            pool.acquire()
        assert exc.value.error == "provider_exhausted"
        assert exc.value.code == 429
        assert exc.value.retry_after is not None
        leases[0].release()
        lease = pool.acquire()
        assert lease.burst
        lease.release()

    def test_named_full_account_is_busy(self) -> None:
        pool = _pool(normal_slots=2, burst_slots=3)
        for _ in range(3):
            pool.acquire(account="devin-1")
        with pytest.raises(ScheduleRefused) as exc:
            pool.acquire(account="devin-1")
        assert exc.value.error == "account_busy"
        assert exc.value.code == 409

    def test_release_idempotent_restores_capacity(self) -> None:
        pool = _pool(normal_slots=1, burst_slots=1)
        lease = pool.acquire()
        lease.release()
        lease.release()
        assert lease.released
        assert pool.active_count == 0
        pool.acquire().release()
        assert pool.active_count == 0

    def test_context_manager_releases_on_exception(self) -> None:
        pool = _pool()
        with pytest.raises(RuntimeError), pool.acquire() as lease:
            raise RuntimeError("boom")
        assert lease.released
        assert pool.active_count == 0

    def test_acquire_touches_last_used(self) -> None:
        reg = _registry()
        pool = DevinAccountPool(reg)
        with pool.acquire():
            pass
        assert reg.get("devin-1") is not None
        assert reg.get("devin-1").last_used_at is not None  # type: ignore[union-attr]


class TestAsync:
    async def test_gather_no_oversell(self) -> None:
        pool = _pool()
        granted: list[int] = []

        async def worker() -> str:
            try:
                async with pool.acquire() as lease:
                    granted.append(lease.slot)
                    await asyncio.sleep(0.01)
                    return "ok"
            except ScheduleRefused:
                return "refused"

        results = await asyncio.gather(*(worker() for _ in range(20)))
        assert results.count("ok") == DEFAULT_BURST_SLOTS
        assert results.count("refused") == 20 - DEFAULT_BURST_SLOTS
        assert sorted(granted) == list(range(1, DEFAULT_BURST_SLOTS + 1))
        assert pool.active_count == 0

    async def test_cancellation_releases_slot(self) -> None:
        pool = _pool()
        started = asyncio.Event()

        async def worker() -> None:
            async with pool.acquire():
                started.set()
                await asyncio.sleep(60)

        task = asyncio.create_task(worker())
        await started.wait()
        assert pool.active_count == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pool.active_count == 0

    async def test_async_exception_releases_slot(self) -> None:
        pool = _pool()
        with pytest.raises(RuntimeError):
            async with pool.acquire():
                raise RuntimeError("boom")
        assert pool.active_count == 0


class TestRaceSafety:
    def test_thread_stampede_grants_exactly_burst(self) -> None:
        pool = _pool()
        n = 64
        barrier = threading.Barrier(n)
        outcomes: list[str] = []
        held: list[SlotLease] = []

        def worker() -> None:
            barrier.wait()
            try:
                lease = pool.acquire()
            except ScheduleRefused:
                outcomes.append("refused")
                return
            held.append(lease)
            outcomes.append("granted")

        with ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(lambda _: worker(), range(n)))

        assert outcomes.count("granted") == DEFAULT_BURST_SLOTS
        assert outcomes.count("refused") == n - DEFAULT_BURST_SLOTS
        assert pool.active_count == DEFAULT_BURST_SLOTS
        for lease in held:
            lease.release()
        assert pool.active_count == 0


class TestCooldown:
    def test_rate_limited_cools_down_then_recovers(self) -> None:
        now = [datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)]
        reg = _registry()
        pool = DevinAccountPool(reg, clock=lambda: now[0])

        acct = pool.report_failure("rate_limited", retry_after=300.0)
        assert acct.status == "cooling"
        assert acct.last_error == "rate_limited"
        assert acct.cooldown_until == "2026-09-14T12:05:00+00:00"

        decision = pool.decide(provider="devin")
        assert decision.error == "provider_exhausted"
        assert decision.retry_after == pytest.approx(300.0)
        with pytest.raises(ScheduleRefused) as exc:
            pool.acquire(account="devin-1")
        assert exc.value.error == "account_unavailable"
        assert exc.value.retry_after == pytest.approx(300.0)

        now[0] += timedelta(seconds=301)
        decision = pool.decide(provider="devin")
        assert decision.account is not None
        assert decision.account.status == "active"
        assert reg.get("devin-1") is not None
        assert reg.get("devin-1").status == "active"  # type: ignore[union-attr]

    def test_auth_invalid_marks_invalid(self) -> None:
        pool = _pool()
        acct = pool.report_failure("auth_invalid")
        assert acct.status == "invalid"
        assert acct.last_error == "auth_invalid"
        decision = pool.decide(provider="devin")
        assert decision.error == "provider_exhausted"
        assert decision.retry_after is None
        with pytest.raises(ScheduleRefused) as exc:
            pool.acquire(account="devin-1")
        assert exc.value.error == "account_unavailable"

    def test_auth_failure_can_cooldown_when_forced(self) -> None:
        pool = _pool()
        acct = pool.report_failure("auth_invalid", status="cooling", retry_after=60.0)
        assert acct.status == "cooling"
        decision = pool.decide(provider="devin")
        assert decision.error == "provider_exhausted"
        assert decision.retry_after is not None
        assert 0 < decision.retry_after <= 60.0

    def test_provider_error_uses_default_cooldown(self) -> None:
        now = [datetime(2026, 9, 14, 0, 0, 0, tzinfo=UTC)]
        pool = DevinAccountPool(_registry(), clock=lambda: now[0])
        acct = pool.report_failure("provider_error")
        assert acct.status == "cooling"
        assert acct.last_error == "provider_error"
        assert acct.cooldown_until is not None
        until = datetime.fromisoformat(acct.cooldown_until)
        assert (until - now[0]).total_seconds() == pytest.approx(900.0)

    def test_report_failure_without_account(self) -> None:
        pool = DevinAccountPool(InMemoryAccountRegistry())
        with pytest.raises(KeyError):
            pool.report_failure("rate_limited")


class TestConfig:
    def test_default_policy_is_four_five_eight(self) -> None:
        pool = _pool()
        assert pool.normal_slots == 4
        assert pool.soft_ceiling == 5
        assert pool.burst_slots == 8

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SBX_DEVIN_NORMAL_SLOTS", "2")
        monkeypatch.setenv("SBX_DEVIN_SOFT_CEILING", "3")
        monkeypatch.setenv("SBX_DEVIN_BURST_SLOTS", "4")
        monkeypatch.setenv("SBX_DEVIN_COOLDOWN_S", "30")
        monkeypatch.setenv("SBX_DEVIN_ACCOUNT_ID", "devin-1")
        reg = _registry(_account("devin-1"), _account("devin-2"))
        pool = DevinAccountPool(reg)
        assert pool.normal_slots == 2
        assert pool.soft_ceiling == 3
        assert pool.burst_slots == 4
        account = pool.account
        assert account is not None and account.id == "devin-1"

    def test_ctor_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SBX_DEVIN_NORMAL_SLOTS", "2")
        pool = _pool(normal_slots=5, burst_slots=9)
        assert pool.normal_slots == 5
        assert pool.soft_ceiling == DEFAULT_SOFT_CEILING
        assert pool.burst_slots == 9

    def test_invalid_slot_config(self) -> None:
        with pytest.raises(ValueError):
            _pool(normal_slots=8, burst_slots=4)
        with pytest.raises(ValueError):
            _pool(normal_slots=0, burst_slots=1)
        with pytest.raises(ValueError):
            _pool(normal_slots=4, soft_ceiling=3, burst_slots=8)
        with pytest.raises(ValueError):
            _pool(normal_slots=4, soft_ceiling=9, burst_slots=8)

    def test_ambiguous_accounts_need_explicit_id(self) -> None:
        reg = _registry(_account("devin-1"), _account("devin-2"))
        pool = DevinAccountPool(reg)
        assert pool.decide(provider="devin").error == "provider_exhausted"
        assert pool.decide(provider="devin", account="devin-1").error == "account_unavailable"
        pinned = DevinAccountPool(reg, account_id="devin-2")
        decision = pinned.decide(provider="devin")
        assert decision.account is not None
        assert decision.account.id == "devin-2"


class _NoBlobRegistry(InMemoryAccountRegistry):
    def get_credential_blob(self, account_id: str) -> dict[str, Any] | None:
        raise AssertionError("scheduler must not touch credential blobs")


class TestCredentialHygiene:
    def test_pool_never_reads_credential_blob(self) -> None:
        reg = _NoBlobRegistry()
        reg.put(_account())
        reg.put_credential_blob(
            "devin-1",
            {"provider": "devin", "files": {".local/share/devin/credentials.toml": "REDACTED"}},
        )
        pool = DevinAccountPool(reg)
        assert pool.decide(provider="devin").account is not None
        with pool.acquire() as lease:
            assert lease.account.id == "devin-1"
        pool.report_failure("rate_limited", retry_after=1.0)
        assert "REDACTED" not in repr(vars(pool))
