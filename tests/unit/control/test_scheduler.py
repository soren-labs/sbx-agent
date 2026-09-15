"""SOR-63/D1: atomic multi-provider AccountScheduler over a persistent registry.

Behavioral model: an Antigravity 4-account pool first, a Grok 2-account pool
second — all accounts are fake records; no real provider traffic.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from control.accounts import InMemoryAccountStore, PersistentAccountRegistry, parse_iso
from control.ports import Account, Scheduler
from control.run_errors import RunError
from control.scheduler import (
    AccountLease,
    AccountScheduler,
    ScheduleRefused,
    failure_status,
)


@pytest.fixture(autouse=True)
def _clean_sched_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("SBX_MAX_CONCURRENT", "SBX_ACCOUNT_COOLDOWN_S", "SBX_SCHEDULER_RETRY_HINT_S"):
        monkeypatch.delenv(key, raising=False)


def _account(
    id: str,
    provider: str = "antigravity",
    max_concurrent: int = 1,
    **kw: Any,
) -> Account:
    return Account(id=id, provider=provider, label=id, max_concurrent=max_concurrent, **kw)


def _registry(*accounts: Account) -> PersistentAccountRegistry:
    reg = PersistentAccountRegistry(InMemoryAccountStore())
    for acct in accounts:
        reg.put(acct)
    return reg


def _agy_pool(n: int = 4, **kw: Any) -> PersistentAccountRegistry:
    """The Antigravity 4-account pool model."""
    return _registry(*(_account(f"agy-{i}", **kw) for i in range(1, n + 1)))


def _grok_pool(**kw: Any) -> PersistentAccountRegistry:
    """The Grok 2-account pool model."""
    return _registry(*(_account(f"grok-{i}", provider="grok", **kw) for i in range(1, 3)))


def _sched(reg: PersistentAccountRegistry, **kw: Any) -> AccountScheduler:
    return AccountScheduler(reg, **kw)


class TestDecide:
    def test_conforms_to_scheduler_protocol(self) -> None:
        assert isinstance(_sched(_agy_pool()), Scheduler)

    def test_invalid_provider(self) -> None:
        sched = _sched(_agy_pool())
        assert sched.decide(provider="nope").error == "invalid_provider"
        with pytest.raises(ScheduleRefused) as exc:
            sched.acquire(provider="nope")
        assert exc.value.error == "invalid_provider"
        assert exc.value.code == 400

    def test_auto_resolves_pool(self) -> None:
        sched = _sched(_agy_pool())
        decision = sched.decide(provider="antigravity")
        assert decision.account is not None
        assert decision.error is None
        assert decision.account.id.startswith("agy-")
        assert sched.decide(provider="antigravity", account=None).account is not None

    def test_named_account_resolves(self) -> None:
        sched = _sched(_agy_pool())
        decision = sched.decide(provider="antigravity", account="agy-3")
        assert decision.account is not None and decision.account.id == "agy-3"

    def test_named_unknown_or_foreign(self) -> None:
        reg = _agy_pool()
        reg.put(_account("grok-1", provider="grok"))
        sched = _sched(reg)
        assert sched.decide(provider="antigravity", account="missing").error == (
            "account_unavailable"
        )
        # A grok account named on an antigravity request is unavailable.
        assert sched.decide(provider="antigravity", account="grok-1").error == (
            "account_unavailable"
        )

    def test_empty_provider_is_exhausted(self) -> None:
        sched = _sched(_registry())
        decision = sched.decide(provider="antigravity")
        assert decision.error == "provider_exhausted"
        assert decision.retry_after is not None
        with pytest.raises(ScheduleRefused) as exc:
            sched.acquire(provider="antigravity")
        assert exc.value.error == "provider_exhausted"
        assert exc.value.code == 429

    def test_decide_is_consultative(self) -> None:
        sched = _sched(_agy_pool())
        for _ in range(10):
            assert sched.decide(provider="antigravity").account is not None
        assert sched.active_count == 0


class TestAutoLru:
    def test_lru_rotates_across_pool(self) -> None:
        sched = _sched(_agy_pool())
        seen = [sched.acquire(provider="antigravity").account.id for _ in range(4)]
        assert sorted(seen) == ["agy-1", "agy-2", "agy-3", "agy-4"]

    def test_least_recently_used_wins(self) -> None:
        now = [datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)]
        reg = _agy_pool()
        sched = _sched(reg, clock=lambda: now[0])

        def take() -> str:
            lease = sched.acquire(provider="antigravity")
            account_id = lease.account.id
            lease.release()
            now[0] += timedelta(seconds=1)
            return account_id

        # Seed last_used_at on all four accounts in order.
        assert [take() for _ in range(4)] == ["agy-1", "agy-2", "agy-3", "agy-4"]
        # Next pick is the least recently used.
        assert take() == "agy-1"
        assert take() == "agy-2"

    def test_exhausted_when_all_slots_taken(self) -> None:
        now = [datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)]
        sched = _sched(_agy_pool(), clock=lambda: now[0])
        leases = []
        for _ in range(4):
            leases.append(sched.acquire(provider="antigravity"))
            now[0] += timedelta(seconds=1)
        decision = sched.decide(provider="antigravity")
        assert decision.error == "provider_exhausted"
        assert decision.retry_after is not None
        leases[0].release()
        # The released account has the oldest last_used_at → LRU re-picks it.
        assert sched.acquire(provider="antigravity").account.id == leases[0].account.id
        for lease in leases[1:]:
            lease.release()


class TestNamedAccount:
    def test_named_acquire_takes_slot_on_that_account(self) -> None:
        sched = _sched(_agy_pool())
        lease = sched.acquire(provider="antigravity", account="agy-2")
        assert lease.account.id == "agy-2"
        assert sched.running_count("agy-2") == 1
        lease.release()

    def test_named_full_is_account_busy(self) -> None:
        sched = _sched(_agy_pool(max_concurrent=1))
        sched.acquire(provider="antigravity", account="agy-1")
        with pytest.raises(ScheduleRefused) as exc:
            sched.acquire(provider="antigravity", account="agy-1")
        assert exc.value.error == "account_busy"
        assert exc.value.code == 409
        # …while auto fails over to a sibling.
        lease = sched.acquire(provider="antigravity")
        assert lease.account.id != "agy-1"
        lease.release()

    def test_named_unavailable_statuses(self) -> None:
        reg = _agy_pool()
        reg.mark_status("agy-1", "cooling", cooldown_until="2999-01-01T00:00:00Z")
        reg.mark_status("agy-2", "invalid", last_error="auth_invalid")
        reg.mark_status("agy-3", "disabled")
        sched = _sched(reg)
        for account_id in ("agy-1", "agy-2", "agy-3"):
            with pytest.raises(ScheduleRefused) as exc:
                sched.acquire(provider="antigravity", account=account_id)
            assert exc.value.error == "account_unavailable"
            assert exc.value.code == 409
        # Cooling carries its remaining time; invalid/disabled carry no hint.
        with pytest.raises(ScheduleRefused) as exc:
            sched.acquire(provider="antigravity", account="agy-1")
        assert exc.value.retry_after is not None
        with pytest.raises(ScheduleRefused) as exc:
            sched.acquire(provider="antigravity", account="agy-2")
        assert exc.value.retry_after is None


class TestPerAccountSlots:
    def test_per_account_max_concurrent(self) -> None:
        reg = _grok_pool(max_concurrent=2)
        sched = _sched(reg)
        leases = [sched.acquire(provider="grok") for _ in range(4)]
        assert sched.active_count == 4
        assert sched.running_count("grok-1") == 2
        assert sched.running_count("grok-2") == 2
        assert [lease.slot for lease in leases].count(1) == 2
        assert [lease.slot for lease in leases].count(2) == 2
        with pytest.raises(ScheduleRefused) as exc:
            sched.acquire(provider="grok")
        assert exc.value.error == "provider_exhausted"
        for lease in leases:
            lease.release()
        assert sched.active_count == 0

    def test_zero_or_negative_max_concurrent_means_one(self) -> None:
        reg = _registry(_account("a1", max_concurrent=0))
        sched = _sched(reg)
        sched.acquire(provider="antigravity")
        with pytest.raises(ScheduleRefused):
            sched.acquire(provider="antigravity")


class TestGlobalCap:
    def test_global_cap_across_providers(self) -> None:
        reg = _agy_pool()
        reg.put(_account("grok-1", provider="grok"))
        sched = _sched(reg, max_global=3)
        leases = [
            sched.acquire(provider="antigravity"),
            sched.acquire(provider="antigravity"),
            sched.acquire(provider="grok"),
        ]
        with pytest.raises(ScheduleRefused) as exc:
            sched.acquire(provider="antigravity")
        assert exc.value.error == "concurrency_limit"
        assert exc.value.code == 429
        assert exc.value.retry_after is not None
        # Named requests respect the global cap too.
        with pytest.raises(ScheduleRefused) as exc:
            sched.acquire(provider="antigravity", account="agy-4")
        assert exc.value.error == "concurrency_limit"
        leases[0].release()
        assert sched.acquire(provider="antigravity").account is not None

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SBX_MAX_CONCURRENT", "1")
        sched = _sched(_agy_pool())
        sched.acquire(provider="antigravity")
        with pytest.raises(ScheduleRefused) as exc:
            sched.acquire(provider="antigravity")
        assert exc.value.error == "concurrency_limit"


class TestAtomicity:
    def test_thread_stampede_no_oversell(self) -> None:
        reg = _agy_pool()
        sched = _sched(reg)
        n = 32
        barrier = threading.Barrier(n)
        held: list[AccountLease] = []
        refused = 0
        lock = threading.Lock()

        def worker() -> None:
            nonlocal refused
            barrier.wait()
            try:
                lease = sched.acquire(provider="antigravity")
            except ScheduleRefused:
                with lock:
                    refused += 1
                return
            with lock:
                held.append(lease)

        with ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(lambda _: worker(), range(n)))

        assert len(held) == 4  # 4 accounts × max_concurrent 1
        assert refused == n - 4
        assert len({lease.account.id for lease in held}) == 4
        for lease in held:
            lease.release()
        assert sched.active_count == 0

    async def test_async_gather_no_oversell(self) -> None:
        sched = _sched(_grok_pool())

        async def worker() -> str:
            try:
                async with sched.acquire(provider="grok"):
                    await asyncio.sleep(0.01)
                    return "ok"
            except ScheduleRefused:
                return "refused"

        results = await asyncio.gather(*(worker() for _ in range(10)))
        assert results.count("ok") == 2
        assert results.count("refused") == 8
        assert sched.active_count == 0

    async def test_cancellation_releases_slot(self) -> None:
        sched = _sched(_grok_pool())
        started = asyncio.Event()

        async def worker() -> None:
            async with sched.acquire(provider="grok"):
                started.set()
                await asyncio.sleep(60)

        task = asyncio.create_task(worker())
        await started.wait()
        assert sched.active_count == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sched.active_count == 0

    def test_release_idempotent(self) -> None:
        sched = _sched(_grok_pool())
        lease = sched.acquire(provider="grok")
        lease.release()
        lease.release()
        assert lease.released
        assert sched.active_count == 0

    def test_context_manager_releases_on_exception(self) -> None:
        sched = _sched(_grok_pool())
        with pytest.raises(RuntimeError), sched.acquire(provider="grok") as lease:
            raise RuntimeError("boom")
        assert lease.released
        assert sched.active_count == 0


class TestCooldownFailover:
    def test_rate_limited_cools_and_fails_over(self) -> None:
        now = [datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)]
        reg = _agy_pool()
        sched = _sched(reg, clock=lambda: now[0])

        lease = sched.acquire(provider="antigravity")
        bad = lease.account.id
        lease.release()
        acct = sched.report_failure(bad, "rate_limited", retry_after=300.0)
        assert acct.status == "cooling"
        assert acct.cooldown_until == "2026-09-15T12:05:00+00:00"
        assert acct.last_error == "rate_limited"

        # Failover: auto picks another account while bad cools.
        assert sched.acquire(provider="antigravity").account.id != bad

    def test_cooldown_expiry_recovers_lazily(self) -> None:
        now = [datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)]
        reg = _agy_pool()
        sched = _sched(reg, clock=lambda: now[0])
        sched.report_failure("agy-1", "rate_limited", retry_after=60.0)
        now[0] += timedelta(seconds=61)
        assert sched.decide(provider="antigravity", account="agy-1").account is not None
        assert reg.get("agy-1").status == "active"  # type: ignore[union-attr]

    def test_all_cooling_exhausted_hint_is_min_remaining(self) -> None:
        now = [datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)]
        reg = _grok_pool()
        sched = _sched(reg, clock=lambda: now[0])
        sched.report_failure("grok-1", "rate_limited", retry_after=120.0)
        sched.report_failure("grok-2", "rate_limited", retry_after=300.0)
        decision = sched.decide(provider="grok")
        assert decision.error == "provider_exhausted"
        assert decision.retry_after == pytest.approx(120.0)

    def test_auth_invalid_marks_invalid_without_hint(self) -> None:
        reg = _grok_pool()
        sched = _sched(reg)
        acct = sched.report_failure("grok-1", "auth_invalid")
        assert acct.status == "invalid"
        sched.report_failure("grok-2", "auth_invalid")
        decision = sched.decide(provider="grok")
        assert decision.error == "provider_exhausted"
        # An all-invalid pool has no automatic retry time.
        assert decision.retry_after is None

    def test_report_run_error_uses_sor82_taxonomy(self) -> None:
        now = [datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)]
        reg = _agy_pool()
        sched = _sched(reg, clock=lambda: now[0])

        err = RunError(
            "provider_unavailable", "provider", "503 upstream", retryable=True, retry_after=45.0
        )
        acct = sched.report_run_error("agy-1", err)
        assert acct.status == "cooling"
        assert acct.cooldown_until == "2026-09-15T12:00:45+00:00"

        # Dict payloads decode the same way (as persisted on run records).
        acct = sched.report_run_error(
            "agy-2", {"code": "auth_invalid", "source": "provider", "message": "401"}
        )
        assert acct.status == "invalid"

        # Non-account-health codes keep the account schedulable but record
        # the short error code.
        acct = sched.report_run_error(
            "agy-3", RunError("timeout", "runtime", "turn exceeded", retryable=True)
        )
        assert acct.status == "active"
        assert acct.last_error == "timeout"

        assert sched.report_run_error("agy-4", None).status == "active"
        with pytest.raises(KeyError):
            sched.report_run_error("missing", err)

    def test_ok_recovers_account_early(self) -> None:
        reg = _agy_pool()
        sched = _sched(reg)
        sched.report_failure("agy-1", "rate_limited", retry_after=900.0)
        acct = sched.report_failure("agy-1", "ok")
        assert acct.status == "active"
        assert acct.cooldown_until is None
        assert acct.last_error is None

    def test_forced_cooldown_override(self) -> None:
        sched = _sched(_agy_pool())
        acct = sched.report_failure("agy-1", "auth_invalid", status="cooling", retry_after=30.0)
        assert acct.status == "cooling"
        assert acct.last_error == "auth_invalid"

    def test_report_unknown_account(self) -> None:
        sched = _sched(_agy_pool())
        with pytest.raises(KeyError):
            sched.report_failure("missing", "rate_limited")

    def test_failure_status_mapping(self) -> None:
        assert failure_status("rate_limited") == "cooling"
        assert failure_status("provider_unavailable") == "cooling"
        assert failure_status("quota_exhausted") == "cooling"
        assert failure_status("model_capacity") == "cooling"
        assert failure_status("provider_error") == "cooling"
        assert failure_status("unknown") == "cooling"
        assert failure_status("auth_invalid") == "invalid"
        for kind in (
            "ok",
            "model_unavailable",
            "runtime_error",
            "event_parse_error",
            "timeout",
            "cancelled",
        ):
            assert failure_status(kind) is None


class TestExternalRunning:
    def test_sessions_derived_count_limits_slots(self) -> None:
        reg = _agy_pool()
        live = {"agy-1": 1}
        sched = _sched(reg, external_running=lambda aid: live.get(aid, 0))
        # agy-1 is at cap via sessions, not leases → skipped by auto.
        assert sched.acquire(provider="antigravity").account.id != "agy-1"
        with pytest.raises(ScheduleRefused) as exc:
            sched.acquire(provider="antigravity", account="agy-1")
        assert exc.value.error == "account_busy"

    def test_lease_and_session_not_double_counted(self) -> None:
        reg = _agy_pool(max_concurrent=2)
        live = {"agy-1": 0}
        sched = _sched(reg, external_running=lambda aid: live.get(aid, 0))
        sched.acquire(provider="antigravity", account="agy-1")
        sched.acquire(provider="antigravity", account="agy-1")
        # The two leases cap the account even though sessions report fewer.
        live["agy-1"] = 1
        with pytest.raises(ScheduleRefused):
            sched.acquire(provider="antigravity", account="agy-1")


class TestRegistryBinding:
    def test_registry_running_count_reflects_leases(self) -> None:
        reg = _agy_pool()
        sched = _sched(reg)
        lease = sched.acquire(provider="antigravity", account="agy-2")
        assert reg.running_count("agy-2") == 1
        lease.release()
        assert reg.running_count("agy-2") == 0

    def test_acquire_touches_last_used(self) -> None:
        reg = _agy_pool()
        sched = _sched(reg)
        with sched.acquire(provider="antigravity", account="agy-1"):
            pass
        assert reg.get("agy-1").last_used_at is not None  # type: ignore[union-attr]

    def test_removed_account_between_pick_and_touch(self) -> None:
        class GoneOnTouch(PersistentAccountRegistry):
            def touch(self, account_id: str, used_at: str) -> None:
                raise KeyError(account_id)

        reg = GoneOnTouch(InMemoryAccountStore())
        reg.put(_account("agy-1"))
        sched = _sched(reg)
        # The account vanished between pick and touch → refusal, and the
        # half-granted slot is rolled back instead of leaking.
        with pytest.raises(ScheduleRefused) as exc:
            sched.acquire(provider="antigravity", account="agy-1")
        assert exc.value.error == "account_unavailable"
        assert sched.active_count == 0
        assert sched.running_count("agy-1") == 0


class _NoBlobStore(InMemoryAccountStore):
    def get_blob(self, account_id: str) -> dict[str, Any] | None:
        raise AssertionError("scheduler must not touch credential blobs")


class TestCredentialHygiene:
    def test_scheduler_never_reads_credential_blob(self) -> None:
        reg = PersistentAccountRegistry(_NoBlobStore())
        reg.put(_account("agy-1"))
        reg.put_credential_blob(
            "agy-1",
            {"provider": "antigravity", "files": {"creds": "REDACTED"}},
        )
        sched = _sched(reg)
        assert sched.decide(provider="antigravity").account is not None
        with sched.acquire(provider="antigravity") as lease:
            assert lease.account.id == "agy-1"
        sched.report_failure("agy-1", "rate_limited", retry_after=1.0)
        assert "REDACTED" not in repr(vars(sched))


def test_ctor_args_beat_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """SBX_* env vars are defaults only; explicit ctor args win."""
    monkeypatch.setenv("SBX_ACCOUNT_COOLDOWN_S", "1")
    before = datetime.now(UTC)
    sched = _sched(_agy_pool(), cooldown_s=600.0)
    acct = sched.report_failure("agy-1", "rate_limited")
    assert acct.cooldown_until is not None
    remaining = (parse_iso(acct.cooldown_until) - before).total_seconds()  # type: ignore[operator]
    assert remaining > 500.0
