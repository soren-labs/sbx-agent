"""Atomic multi-provider account scheduler (SOR-63/D1).

Generalizes ``control.devin_pool`` (single Devin account, tiered slots) and
the WP0 ``InMemoryScheduler`` fake into the real multi-account core over any
``ports.AccountRegistry``:

* ``account="auto"`` → LRU pick among the provider's ``active`` accounts
  with a free slot; ``provider_exhausted{retry_after}`` when none qualify.
* named ``account`` → ``account_unavailable`` (missing / wrong provider /
  not ``active``) or ``account_busy`` (no free slot).
* per-account slots = ``Account.max_concurrent``; global cap =
  ``SBX_MAX_CONCURRENT`` (default 8) → ``concurrency_limit``.
* ``acquire`` performs the check-and-take under one lock — concurrent async
  tasks and threads cannot oversell a slot. Leases release idempotently.
* Health feedback consumes the SOR-82 ``RunError`` taxonomy:
  ``rate_limited`` / ``provider_unavailable`` / ``quota_exhausted`` /
  ``model_capacity`` (and the legacy adapter kinds ``provider_error`` /
  ``unknown``) → ``cooling`` until ``now + retry_after`` (default
  ``cooldown_s``); ``auth_invalid`` → ``invalid``; runtime / control /
  telemetry codes record ``last_error`` without a status change. A
  ``cooling`` account recovers to ``active`` once ``cooldown_until`` passes
  — lazily on the next pick, or proactively via the reaper sweep.

Credential material never enters this module: only account ids and the
frozen ``Account`` records are handled.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, get_args

from control.accounts import cooldown_expired, is_valid_account_id, iso_utc, parse_iso
from control.config import TERMINAL_STATUSES, env_float, env_int
from control.ports import Account, AccountRegistry, ProviderId, ScheduleDecision
from control.run_errors import RunError

PROVIDERS: tuple[str, ...] = get_args(ProviderId)

DEFAULT_MAX_GLOBAL = 8  # design v2 §3.3: SBX_MAX_CONCURRENT
DEFAULT_COOLDOWN_S = 900.0  # rate_limited → cooling 15 min
DEFAULT_RETRY_HINT_S = 60.0

Clock = Callable[[], datetime]

# HTTP status per canonical subcode (api.yaml / api-v1.yaml ErrorBody).
_ERROR_HTTP_CODE = {
    "invalid_provider": 400,
    "account_unavailable": 409,
    "account_busy": 409,
    "provider_exhausted": 429,
    "concurrency_limit": 429,
}

# Failure kind → account status transition (design v2 §3.3 health feedback +
# SOR-82 run-error taxonomy). Kinds not listed leave the status untouched.
_COOLING_KINDS = frozenset(
    {
        "rate_limited",
        "provider_unavailable",
        "quota_exhausted",
        "model_capacity",
        # AgentAdapter ``Health`` values (pre-SOR-82 taxonomy).
        "provider_error",
        "unknown",
    }
)
_INVALID_KINDS = frozenset({"auth_invalid"})


def failure_status(kind: str) -> str | None:
    """Map a failure kind (RunError code or adapter ``Health``) to a status.

    ``None`` means "not an account-health signal" — ``model_unavailable``,
    ``runtime_error``, ``event_parse_error``, ``timeout``, ``cancelled`` and
    ``ok`` do not cool or invalidate the account.
    """
    if kind in _INVALID_KINDS:
        return "invalid"
    if kind in _COOLING_KINDS:
        return "cooling"
    return None


def session_running_source(store: Any) -> Callable[[str], int]:
    """Build an ``external_running`` hook backed by a ``SessionStore``.

    Counts non-terminal session records tagged ``account_id=<id>`` — the
    design-v2-§3.3 "derive running from the sessions store" source. It keeps
    per-account and global slot accounting truthful across control-plane
    restarts, where in-process leases are gone but earlier sandboxes are
    still live. Sessions holding an in-process lease appear in both counts;
    ``running_count`` takes the max and never double-counts them.
    """

    def count(account_id: str) -> int:
        return sum(
            1
            for rec in store.list_all()
            if rec.status not in TERMINAL_STATUSES
            and (rec.sandbox_tags or {}).get("account_id") == account_id
        )

    return count


class ScheduleRefused(Exception):
    """``acquire`` refusal carrying a frozen canonical error subcode."""

    def __init__(self, error: str, *, retry_after: float | None = None) -> None:
        super().__init__(error)
        self.error = error
        self.code = _ERROR_HTTP_CODE.get(error, 429)
        self.retry_after = retry_after


class AccountLease:
    """One held slot on one account.

    Release with ``release()`` or a context manager — ``with
    scheduler.acquire()`` or ``async with scheduler.acquire()``.
    ``__aexit__`` awaits nothing, so a cancelled task still releases its
    slot. Release is idempotent.
    """

    def __init__(self, scheduler: AccountScheduler, account: Account, slot: int) -> None:
        self._scheduler = scheduler
        self.account = account
        self.slot = slot  # depth of held slots on this account at grant
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        """Return the slot; safe to call more than once."""
        self._scheduler._release(self)

    def __enter__(self) -> AccountLease:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    async def __aenter__(self) -> AccountLease:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.release()


class AccountScheduler:
    """Multi-provider, multi-account ``ports.Scheduler`` + slot pool.

    The check-and-take critical section holds ``self._lock`` and never
    awaits, so ``acquire`` is atomic for both threads and async tasks.
    ``_slots`` counts leases held in this process; ``external_running`` is an
    optional ``account_id -> live sessions`` hook for capacity held outside
    the lease path (design v2 §3.3 derives running counts from sessions) —
    the effective count is ``max(leases, external)`` so a session holding a
    lease is never double-counted.
    """

    def __init__(
        self,
        registry: AccountRegistry,
        *,
        max_global: int | None = None,
        cooldown_s: float | None = None,
        retry_hint_s: float | None = None,
        providers: tuple[str, ...] = PROVIDERS,
        external_running: Callable[[str], int] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._registry = registry
        self._providers = frozenset(providers)
        self.max_global = (
            max_global
            if max_global is not None
            else env_int("SBX_MAX_CONCURRENT", DEFAULT_MAX_GLOBAL)
        )
        self._cooldown_s = (
            cooldown_s
            if cooldown_s is not None
            else env_float("SBX_ACCOUNT_COOLDOWN_S", DEFAULT_COOLDOWN_S)
        )
        self._retry_hint_s = (
            retry_hint_s
            if retry_hint_s is not None
            else env_float("SBX_SCHEDULER_RETRY_HINT_S", DEFAULT_RETRY_HINT_S)
        )
        if self.max_global < 1:
            raise ValueError(f"invalid max_global={self.max_global}")
        self._external_running = external_running
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._slots: dict[str, int] = {}
        bind = getattr(registry, "bind_running", None)
        if callable(bind):
            bind(self.running_count)

    @property
    def registry(self) -> AccountRegistry:
        return self._registry

    @property
    def active_count(self) -> int:
        """Total slots held across all accounts/providers."""
        with self._lock:
            return sum(self._slots.values())

    def running_count(self, account_id: str) -> int:
        """Effective live sessions on the account: ``max(leases, external)``."""
        with self._lock:
            return self._running_locked(account_id)

    def decide(self, *, provider: str, account: str | None = "auto") -> ScheduleDecision:
        """Frozen Scheduler surface: consultative pick, reserves no slot."""
        with self._lock:
            acct, refusal = self._pick_locked(provider=provider, account=account)
        if refusal is not None:
            return ScheduleDecision(error=refusal.error, retry_after=refusal.retry_after)
        assert acct is not None
        return ScheduleDecision(account=acct)

    def acquire(self, *, provider: str, account: str | None = "auto") -> AccountLease:
        """Atomically take one slot or raise ``ScheduleRefused``."""
        with self._lock:
            acct, refusal = self._pick_locked(provider=provider, account=account)
            if refusal is not None:
                raise refusal
            assert acct is not None
            self._slots[acct.id] = self._slots.get(acct.id, 0) + 1
            try:
                self._registry.touch(acct.id, iso_utc(self._clock()))
            except KeyError:
                self._slots[acct.id] -= 1
                raise ScheduleRefused("account_unavailable") from None
            except Exception:
                self._slots[acct.id] -= 1
                raise
            return AccountLease(self, acct, self._slots[acct.id])

    def report_failure(
        self,
        account_id: str,
        kind: str,
        *,
        retry_after: float | None = None,
        status: str | None = None,
    ) -> Account:
        """Feed a provider-side failure back into the account record.

        ``rate_limited`` / ``provider_unavailable`` / ``quota_exhausted`` /
        ``model_capacity`` (and legacy ``provider_error`` / ``unknown``) →
        ``cooling`` until ``now + retry_after`` (default ``cooldown_s``);
        ``auth_invalid`` → ``invalid``; ``ok`` → recover to ``active``;
        anything else records ``last_error`` without a status change. Pass
        ``status="cooling"`` to force a timed cooldown for any kind. Raises
        ``KeyError`` for an unknown account — a non-conformant id can never
        name one (SOR-105).
        """
        with self._lock:
            if not is_valid_account_id(account_id):
                raise KeyError(account_id)
            acct = self._registry.get(account_id)
            if acct is None:
                raise KeyError(account_id)
            new_status = status or failure_status(kind)
            if new_status is None and kind == "ok":
                new_status = "active"
            if new_status is None:
                return self._registry.mark_status(
                    acct.id,
                    acct.status,
                    cooldown_until=acct.cooldown_until,
                    last_error=kind,
                )
            cooldown_until = None
            if new_status == "cooling":
                delay = retry_after if retry_after is not None else self._cooldown_s
                cooldown_until = iso_utc(self._clock() + timedelta(seconds=delay))
            return self._registry.mark_status(
                acct.id,
                new_status,
                cooldown_until=cooldown_until,
                last_error=None if kind == "ok" else kind,
            )

    def report_run_error(
        self, account_id: str, error: RunError | Mapping[str, Any] | None
    ) -> Account:
        """``report_failure`` for the SOR-82 structured ``RunError`` shape.

        Preserves ``retry_after`` from the error. ``None`` or an
        undecodable payload is "no failure" — the account is returned
        unchanged (``KeyError`` still applies for an unknown account).
        """
        err = error if isinstance(error, RunError) else RunError.from_dict(error)
        if err is None:
            with self._lock:
                if not is_valid_account_id(account_id):
                    raise KeyError(account_id)
                acct = self._registry.get(account_id)
                if acct is None:
                    raise KeyError(account_id)
                return acct
        return self.report_failure(account_id, err.code, retry_after=err.retry_after)

    def _release(self, lease: AccountLease) -> None:
        with self._lock:
            if lease._released:
                return
            lease._released = True
            remaining = self._slots.get(lease.account.id, 0) - 1
            if remaining > 0:
                self._slots[lease.account.id] = remaining
            else:
                self._slots.pop(lease.account.id, None)

    # ---------------------------------------------------------- internals

    def _running_locked(self, account_id: str) -> int:
        held = self._slots.get(account_id, 0)
        if self._external_running is None:
            return held
        return max(held, self._external_running(account_id))

    def _global_running_locked(self) -> int:
        ids = set(self._slots)
        if self._external_running is not None:
            ids.update(a.id for a in self._registry.list())
        return sum(self._running_locked(aid) for aid in ids)

    @staticmethod
    def _cap(account: Account) -> int:
        return account.max_concurrent if account.max_concurrent > 0 else 1

    def _refresh_locked(self, acct: Account) -> Account:
        """Auto-recover a ``cooling`` account whose ``cooldown_until`` passed."""
        if not is_valid_account_id(acct.id):
            return acct  # non-conformant stored id — never usable, leave it (SOR-105)
        if cooldown_expired(acct, self._clock()):
            return self._registry.mark_status(acct.id, "active")
        return acct

    def _retry_after_locked(self, acct: Account) -> float | None:
        until = parse_iso(acct.cooldown_until)
        if until is not None:
            return max(0.0, (until - self._clock()).total_seconds())
        if acct.status == "cooling":
            return self._retry_hint_s
        return None  # invalid/disabled: no automatic retry

    def _exhausted_hint_locked(self, accounts: list[Account]) -> float | None:
        """``retry_after`` for ``provider_exhausted`` given the pool's state.

        Earliest known recovery wins: cooling accounts contribute their
        remaining cooldown, slot-full active accounts the generic hint
        (a slot frees whenever a session ends). No accounts at all also
        yields the hint — an import could land at any time. All-invalid /
        all-disabled pools carry no hint.
        """
        if not accounts:
            return self._retry_hint_s
        hints = [hint for acct in accounts if (hint := self._exhausted_hint_for(acct)) is not None]
        return min(hints) if hints else None

    def _exhausted_hint_for(self, acct: Account) -> float | None:
        if acct.status == "cooling":
            return self._retry_after_locked(acct)
        if acct.status == "active" and self._running_locked(acct.id) >= self._cap(acct):
            return self._retry_hint_s
        return None

    def _pick_locked(
        self, *, provider: str, account: str | None
    ) -> tuple[Account | None, ScheduleRefused | None]:
        """Shared decide/acquire check. Caller holds ``self._lock``."""
        if provider not in self._providers:
            return None, ScheduleRefused("invalid_provider")
        if account not in (None, "auto"):
            return self._pick_named_locked(provider, account)
        return self._pick_auto_locked(provider)

    def _pick_named_locked(
        self, provider: str, account_id: str
    ) -> tuple[Account | None, ScheduleRefused | None]:
        # SOR-105: a non-conformant id can never name an account — refuse it
        # here so it never reaches a registry/store path as a lookup miss.
        if not is_valid_account_id(account_id):
            return None, ScheduleRefused("account_unavailable")
        acct = self._registry.get(account_id)
        if acct is None or acct.provider != provider:
            return None, ScheduleRefused("account_unavailable")
        acct = self._refresh_locked(acct)
        if acct.status != "active":
            return None, ScheduleRefused(
                "account_unavailable", retry_after=self._retry_after_locked(acct)
            )
        if self._running_locked(acct.id) >= self._cap(acct):
            return None, ScheduleRefused("account_busy")
        if self._global_running_locked() >= self.max_global:
            return None, ScheduleRefused("concurrency_limit", retry_after=self._retry_hint_s)
        return acct, None

    def _pick_auto_locked(self, provider: str) -> tuple[Account | None, ScheduleRefused | None]:
        accounts = [self._refresh_locked(a) for a in self._registry.list(provider)]
        candidates = [
            a
            for a in accounts
            if a.status == "active"
            and is_valid_account_id(a.id)  # a non-conformant stored id can never serve a session
            and self._running_locked(a.id) < self._cap(a)
        ]
        if not candidates:
            return None, ScheduleRefused(
                "provider_exhausted", retry_after=self._exhausted_hint_locked(accounts)
            )
        if self._global_running_locked() >= self.max_global:
            return None, ScheduleRefused("concurrency_limit", retry_after=self._retry_hint_s)
        # LRU: never-used accounts first, then oldest last_used_at; id breaks ties.
        chosen = min(candidates, key=lambda a: (a.last_used_at or "", a.id))
        return chosen, None


__all__ = [
    "PROVIDERS",
    "AccountLease",
    "AccountScheduler",
    "ScheduleRefused",
    "failure_status",
    "session_running_source",
]
