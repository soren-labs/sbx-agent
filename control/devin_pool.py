"""P2.1 minimal single-Devin-account scheduler/pool (SOR-75).

Drives one Devin Pro account as a small worker pool until the full
multi-provider / multi-account control plane (SOR-63) lands.
``DevinAccountPool.decide`` satisfies the frozen
``control.ports.Scheduler`` Protocol, so ``/v1`` and internal callers can
swap in the SOR-63 implementation without call-site changes.

Slot policy (SOR-73, measured 2026-09-14 on one CLI login):

* ``normal_slots`` (default 4): grants 1..4 — the sustained target.
* ``soft_ceiling`` (default 5): slot 5 is normal headroom, flagged ``soft``.
* ``burst_slots`` (default 8): grants 6..8 are flagged ``burst``.
* Beyond ``burst_slots`` the pool refuses with the canonical
  ``provider_exhausted`` (``account: "auto"``) / ``account_busy`` (named)
  subcodes from ``ports.SCHEDULE_ERRORS`` — the same shape the 429/409
  mapping in api.yaml / api-v1.yaml renders.

Health feedback (design v2 §3.3): ``rate_limited`` / provider errors move
the account to ``cooling`` until ``cooldown_until`` (auto-recovers on the
next pick); ``auth_invalid`` marks it ``invalid``. Cooldown metadata
surfaces as ``retry_after`` on subsequent refusals.

Credential material never enters this module: the pool keeps only the
account id and reaches the account through ``AccountRegistry``; the opaque
credential blob stays behind ``get_credential_blob``.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from control.accounts import iso_utc as _iso
from control.accounts import parse_iso as _parse_iso
from control.config import env_float, env_int
from control.ports import Account, AccountRegistry, ScheduleDecision

# Shared refusal shape + failure→status taxonomy (SOR-63/D1). Re-exported so
# existing ``from control.devin_pool import ScheduleRefused`` keep working.
from control.scheduler import ScheduleRefused, failure_status

DEVIN_PROVIDER = "devin"

DEFAULT_NORMAL_SLOTS = 4
DEFAULT_SOFT_CEILING = 5
DEFAULT_BURST_SLOTS = 8
DEFAULT_COOLDOWN_S = 900.0  # design v2 §3.3: rate_limited → cooling 15 min
DEFAULT_RETRY_HINT_S = 60.0

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SlotLease:
    """One held slot on the Devin account.

    Release with ``release()`` or a context manager — ``with
    pool.acquire()`` or ``async with pool.acquire()``. ``__aexit__`` awaits
    nothing, so a cancelled task still releases its slot. Release is
    idempotent.
    """

    def __init__(self, pool: DevinAccountPool, account: Account, slot: int) -> None:
        self._pool = pool
        self.account = account
        self.slot = slot  # depth of held slots at grant (1..burst_slots)
        self.soft = pool.normal_slots < slot <= pool.soft_ceiling
        self.burst = slot > pool.soft_ceiling
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        """Return the slot; safe to call more than once."""
        self._pool._release(self)

    def __enter__(self) -> SlotLease:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    async def __aenter__(self) -> SlotLease:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.release()


class DevinAccountPool:
    """Single-account ``Scheduler`` + slot pool for provider ``devin``.

    Parameters fall back to ``SBX_DEVIN_*`` env vars, then to the SOR-73
    defaults. The account is resolved lazily through the registry: an
    explicit ``account_id`` / ``SBX_DEVIN_ACCOUNT_ID``, else the single
    ``devin`` account present. Pool slot caps are authoritative for P2.1 —
    ``Account.max_concurrent`` is a SOR-63 concern and ignored here.
    """

    provider = DEVIN_PROVIDER

    def __init__(
        self,
        registry: AccountRegistry,
        *,
        account_id: str | None = None,
        normal_slots: int | None = None,
        soft_ceiling: int | None = None,
        burst_slots: int | None = None,
        cooldown_s: float | None = None,
        retry_hint_s: float | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._registry = registry
        self._account_id = account_id or os.environ.get("SBX_DEVIN_ACCOUNT_ID") or None
        self.normal_slots = (
            normal_slots
            if normal_slots is not None
            else env_int("SBX_DEVIN_NORMAL_SLOTS", DEFAULT_NORMAL_SLOTS)
        )
        self.burst_slots = (
            burst_slots
            if burst_slots is not None
            else env_int("SBX_DEVIN_BURST_SLOTS", DEFAULT_BURST_SLOTS)
        )
        env_soft = os.environ.get("SBX_DEVIN_SOFT_CEILING")
        if soft_ceiling is not None:
            self.soft_ceiling = soft_ceiling
        elif env_soft not in (None, ""):
            self.soft_ceiling = int(env_soft)
        else:
            self.soft_ceiling = min(max(DEFAULT_SOFT_CEILING, self.normal_slots), self.burst_slots)
        if (
            self.normal_slots < 1
            or self.soft_ceiling < self.normal_slots
            or self.burst_slots < self.soft_ceiling
        ):
            raise ValueError(
                f"invalid slot config: normal_slots={self.normal_slots} "
                f"soft_ceiling={self.soft_ceiling} burst_slots={self.burst_slots}"
            )
        self._cooldown_s = (
            cooldown_s
            if cooldown_s is not None
            else env_float("SBX_DEVIN_COOLDOWN_S", DEFAULT_COOLDOWN_S)
        )
        self._retry_hint_s = (
            retry_hint_s
            if retry_hint_s is not None
            else env_float("SBX_DEVIN_RETRY_HINT_S", DEFAULT_RETRY_HINT_S)
        )
        self._clock = clock or _utcnow
        self._lock = threading.Lock()
        self._active = 0

    @property
    def active_count(self) -> int:
        """Currently held slots (0..burst_slots)."""
        with self._lock:
            return self._active

    @property
    def account(self) -> Account | None:
        """The single configured Devin account, if resolvable."""
        with self._lock:
            return self._resolve_locked()

    def decide(self, *, provider: str, account: str | None = "auto") -> ScheduleDecision:
        """Frozen Scheduler surface: consultative pick, reserves no slot.

        ``decide`` mirrors the refusal shape so ``Scheduler``-typed callers
        work unchanged; callers that actually need the slot must use
        ``acquire``, which performs the atomic check-and-take.
        """
        with self._lock:
            acct, refusal = self._pick_locked(provider=provider, account=account)
        if refusal is not None:
            return ScheduleDecision(error=refusal.error, retry_after=refusal.retry_after)
        assert acct is not None
        return ScheduleDecision(account=acct)

    def acquire(self, *, provider: str = DEVIN_PROVIDER, account: str | None = "auto") -> SlotLease:
        """Atomically take one slot or raise ``ScheduleRefused``.

        Safe under concurrent async tasks and threads: the check-and-take
        critical section holds ``self._lock`` and never awaits. Slots are
        counted 1..burst_slots; the slot above ``normal_slots`` through
        ``soft_ceiling`` is marked ``soft`` and grants above ``soft_ceiling``
        are marked ``burst``.
        """
        with self._lock:
            acct, refusal = self._pick_locked(provider=provider, account=account)
            if refusal is not None:
                raise refusal
            assert acct is not None
            self._active += 1
            try:
                self._registry.touch(acct.id, _iso(self._clock()))
            except KeyError:
                self._active -= 1
                raise ScheduleRefused("account_unavailable") from None
            return SlotLease(self, acct, self._active)

    def report_failure(
        self,
        kind: str,
        *,
        retry_after: float | None = None,
        status: str | None = None,
    ) -> Account:
        """Feed a provider-side failure back into the account record.

        ``rate_limited`` / ``provider_error`` / ``unknown`` → ``cooling``
        until ``now + retry_after`` (default ``cooldown_s``);
        ``auth_invalid`` → ``invalid`` (credential re-import needed, per
        design v2 §3.3). Pass ``status="cooling"`` to force a timed
        cooldown for any kind. While the account is down, its cooldown
        metadata surfaces as ``retry_after`` on refusals. Returns the
        updated ``Account``; KeyError if no devin account is configured.
        """
        with self._lock:
            acct = self._resolve_locked()
            if acct is None:
                raise KeyError("no devin account configured")
            new_status = status or failure_status(kind) or "cooling"
            cooldown_until = None
            if new_status == "cooling":
                delay = retry_after if retry_after is not None else self._cooldown_s
                cooldown_until = _iso(self._clock() + timedelta(seconds=delay))
            return self._registry.mark_status(
                acct.id,
                new_status,
                cooldown_until=cooldown_until,
                last_error=kind,
            )

    def _pick_locked(
        self, *, provider: str, account: str | None
    ) -> tuple[Account | None, ScheduleRefused | None]:
        """Shared decide/acquire check. Caller holds ``self._lock``."""
        if provider != self.provider:
            return None, ScheduleRefused("invalid_provider")
        named = account not in (None, "auto")
        acct = self._resolve_locked()
        if named and (acct is None or acct.id != account):
            return None, ScheduleRefused("account_unavailable")
        if acct is None:
            return None, ScheduleRefused("provider_exhausted", retry_after=self._retry_hint_s)
        acct = self._refresh_locked(acct)
        if acct.status != "active":
            retry = self._retry_after_locked(acct)
            if named:
                return None, ScheduleRefused("account_unavailable", retry_after=retry)
            return None, ScheduleRefused("provider_exhausted", retry_after=retry)
        if self._active >= self.burst_slots:
            if named:
                return None, ScheduleRefused("account_busy")
            return None, ScheduleRefused("provider_exhausted", retry_after=self._retry_hint_s)
        return acct, None

    def _resolve_locked(self) -> Account | None:
        """The pool's one account: pinned id, else the unique devin account."""
        if self._account_id is not None:
            acct = self._registry.get(self._account_id)
            return acct if acct is not None and acct.provider == self.provider else None
        accounts = self._registry.list(self.provider)
        return accounts[0] if len(accounts) == 1 else None

    def _refresh_locked(self, acct: Account) -> Account:
        """Auto-recover a ``cooling`` account whose ``cooldown_until`` passed."""
        if acct.status != "cooling":
            return acct
        until = _parse_iso(acct.cooldown_until)
        if until is not None and until <= self._clock():
            return self._registry.mark_status(acct.id, "active")
        return acct

    def _retry_after_locked(self, acct: Account) -> float | None:
        until = _parse_iso(acct.cooldown_until)
        if until is not None:
            return max(0.0, (until - self._clock()).total_seconds())
        if acct.status == "cooling":
            return self._retry_hint_s
        return None  # invalid/disabled: no automatic retry

    def _release(self, lease: SlotLease) -> None:
        with self._lock:
            if lease._released:
                return
            lease._released = True
            self._active = max(0, self._active - 1)
