"""Modal-only bootstrap wiring for the P2 real /v1 gate.

Nothing is enabled by default. When ``SBX_V1_BOOTSTRAP_KEY`` is injected via
a Modal Secret, seed a hash-only API key plus accounts per P2 Core provider —
codex, devin, antigravity, grok — so the frozen candidate can schedule all
four through the product ``/v1`` path. Devin keeps its P2.1 tiered slot
pool; the other providers get flat per-account slot pools. A provider seeds
one account by default; ``SBX_<PROVIDER>_ACCOUNTS`` (a JSON list of
``{"id", "label"?, "secret_name"?, "slots"?, "models"?}``) seeds a real
multi-account pool — the SOR-63/D2 wiring seam that lets the /v1 gate run
the Antigravity-4 / Grok-2 account pools ahead of D1's persistent
scheduler, which later replaces this module behind the same
``app.state.scheduler`` surface. OpenCode stays deferred and refuses with
``provider_exhausted``, not ``invalid_provider``.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from typing import Any

from control.api_v1.state import (
    PROVIDERS,
    InMemoryAccountRegistry,
    InMemoryApiKeyStore,
)
from control.config import env_int
from control.devin_pool import (
    Clock,
    DevinAccountPool,
    ScheduleRefused,
    SlotLease,
)
from control.ports import Account, ScheduleDecision

# Flat per-account slot cap for non-Devin providers; Devin keeps its
# normal/soft/burst tiers from ``SBX_DEVIN_*``. Overridable per provider via
# ``SBX_<PROVIDER>_SLOTS``.
_DEFAULT_PROVIDER_SLOTS = 4

# Default advertised models per provider; ``SBX_<PROVIDER>_MODELS`` overrides
# (comma-separated). Informational only — scheduling does not gate on models.
# ``/v1`` also uses the first entry as the default model when the resolved
# account advertises none and ``AgentSpec.model`` is omitted.
PROVIDER_DEFAULT_MODELS = {
    "codex": ("gpt-5.6-luna",),
    "devin": ("swe-2-high", "swe-2-medium"),
    "antigravity": ("gemini-3.8-flash-low",),
    "grok": ("grok-4.6",),
}


def _models(provider: str) -> tuple[str, ...]:
    raw = os.environ.get(f"SBX_{provider.upper()}_MODELS")
    if raw is None:
        return PROVIDER_DEFAULT_MODELS[provider]
    return tuple(part.strip() for part in raw.split(",") if part.strip())


class SingleAccountPool(DevinAccountPool):
    """``DevinAccountPool`` retargeted at another provider's seeded account.

    The pool is already a single-account ``Scheduler`` + slot pool; the only
    Devin coupling in the pick path is the class-level ``provider`` tag,
    overridden per instance here. Slot tiers collapse to one flat cap —
    normal/soft/burst are Devin account policy (SOR-73), not provider-generic.
    """

    def __init__(
        self,
        registry: InMemoryAccountRegistry,
        *,
        provider: str,
        account_id: str,
        slots: int,
        clock: Clock | None = None,
        cooldown_s: float | None = None,
    ) -> None:
        super().__init__(
            registry,
            account_id=account_id,
            normal_slots=slots,
            soft_ceiling=slots,
            burst_slots=slots,
            clock=clock,
            cooldown_s=cooldown_s,
        )
        self.provider = provider


class ProviderPool:
    """Per-provider composition of pinned single-account slot pools.

    Stands in for a provider's whole account fleet behind the same
    ``decide``/``acquire``/``report_failure`` surface the /v1 routes
    already consume, so the real multi-account gate runs ahead of D1's
    persistent scheduler. ``auto`` choose-and-take runs under one lock —
    atomic, no oversubscription — and picks the LRU account (never-used
    first, then oldest ``last_used_at``), the same rule the frozen
    ``Scheduler`` contract specifies.
    """

    def __init__(self, pools: dict[str, DevinAccountPool]) -> None:
        if not pools:
            raise ValueError("ProviderPool requires at least one account pool")
        providers = {pool.provider for pool in pools.values()}
        if len(providers) != 1:
            raise ValueError("ProviderPool pools must share one provider")
        self._pools = dict(pools)
        self.provider = providers.pop()
        self._lock = threading.Lock()

    @property
    def active_count(self) -> int:
        """Leases currently held across the provider's accounts."""
        return sum(pool.active_count for pool in self._pools.values())

    @staticmethod
    def _exhausted(hints: list[float]) -> ScheduleRefused:
        """``provider_exhausted`` with the earliest member retry hint."""
        return ScheduleRefused("provider_exhausted", retry_after=min(hints) if hints else None)

    def decide(self, *, provider: str, account: str | None = "auto") -> ScheduleDecision:
        """Consultative decide: answers who would serve, takes no slot."""
        if provider != self.provider:
            return ScheduleDecision(error="invalid_provider")
        if account not in (None, "auto"):
            pool = self._pools.get(account)
            if pool is None:
                return ScheduleDecision(error="account_unavailable")
            return pool.decide(provider=provider, account=account)
        candidates: list[Account] = []
        hints: list[float] = []
        with self._lock:
            for pool in self._pools.values():
                # Members are pinned to one account — query them as ``auto``
                # so an unavailable member reports provider_exhausted with
                # its retry hint rather than a named account_busy.
                decision = pool.decide(provider=provider, account="auto")
                if decision.account is not None:
                    candidates.append(decision.account)
                elif decision.retry_after is not None:
                    hints.append(decision.retry_after)
        if not candidates:
            return ScheduleDecision(
                error="provider_exhausted", retry_after=min(hints) if hints else None
            )
        return ScheduleDecision(account=min(candidates, key=lambda a: a.last_used_at or ""))

    def acquire(self, *, provider: str, account: str | None = "auto") -> SlotLease:
        """Atomic pick-and-take for ``auto``; a named id pins its account."""
        if provider != self.provider:
            raise ScheduleRefused("invalid_provider")
        if account not in (None, "auto"):
            pool = self._pools.get(account)
            if pool is None:
                raise ScheduleRefused("account_unavailable")
            return pool.acquire(provider=provider, account=account)
        with self._lock:
            ordered = sorted(
                self._pools.items(),
                key=lambda item: (item[1].account.last_used_at if item[1].account else "") or "",
            )
            hints: list[float] = []
            for _account_id, pool in ordered:
                try:
                    return pool.acquire(provider=provider, account="auto")
                except ScheduleRefused as exc:
                    if exc.retry_after is not None:
                        hints.append(exc.retry_after)
        raise self._exhausted(hints)

    def report_failure(
        self,
        kind: str,
        *,
        account_id: str | None = None,
        retry_after: float | None = None,
        status: str | None = None,
    ) -> Account:
        """Route a provider failure to the pool owning ``account_id``."""
        if account_id is None or account_id not in self._pools:
            raise KeyError(account_id)
        return self._pools[account_id].report_failure(kind, retry_after=retry_after, status=status)


class BootstrapScheduler:
    """Frozen ``Scheduler`` surface + atomic ``acquire`` across provider pools.

    Seeded provider pools — one account by default, or a ``ProviderPool``
    per provider when ``SBX_<PROVIDER>_ACCOUNTS`` lists several. Contract-
    valid but unseeded providers (``opencode``) refuse ``auto`` picks with
    ``provider_exhausted`` and named picks with ``account_unavailable``;
    unknown ids get ``invalid_provider``. ``report_failure`` routes
    provider health signals to the pool owning the failing account.
    """

    def __init__(
        self,
        pools: dict[str, Any],
        *,
        account_pools: dict[str, DevinAccountPool] | None = None,
    ) -> None:
        self.pools = dict(pools)
        self._account_pools = dict(account_pools or {})

    def decide(self, *, provider: str, account: str | None = "auto") -> ScheduleDecision:
        pool = self.pools.get(provider)
        if pool is not None:
            return pool.decide(provider=provider, account=account)
        if provider not in PROVIDERS:
            return ScheduleDecision(error="invalid_provider")
        if account in (None, "auto"):
            return ScheduleDecision(error="provider_exhausted", retry_after=60.0)
        return ScheduleDecision(error="account_unavailable")

    def acquire(self, *, provider: str, account: str | None = "auto") -> SlotLease:
        pool = self.pools.get(provider)
        if pool is not None:
            return pool.acquire(provider=provider, account=account)
        if provider not in PROVIDERS:
            raise ScheduleRefused("invalid_provider")
        if account in (None, "auto"):
            raise ScheduleRefused("provider_exhausted", retry_after=60.0)
        raise ScheduleRefused("account_unavailable")

    def report_failure(
        self,
        kind: str,
        *,
        account_id: str | None = None,
        retry_after: float | None = None,
        status: str | None = None,
    ) -> Account:
        pool = self._account_pools.get(account_id or "")
        if pool is None:
            raise KeyError(account_id)
        return pool.report_failure(kind, retry_after=retry_after, status=status)


def _seed_account(
    registry: InMemoryAccountRegistry,
    provider: str,
    *,
    default_secret_name: str | None,
    default_slots: int,
    label: str,
    created_at: str,
) -> str:
    """Seed one active account for ``provider``; return the account id.

    ``SBX_<PROVIDER>_ACCOUNT_ID`` / ``SBX_<PROVIDER>_SECRET_NAME`` /
    ``SBX_<PROVIDER>_SLOTS`` override the defaults. ``default_secret_name`` of
    ``None`` resolves to the ``sbx-acct-<account_id>`` convention; ``""``
    attaches no per-account Secret (the provider's default credential path).
    """
    prefix = f"SBX_{provider.upper()}"
    account_id = (os.environ.get(f"{prefix}_ACCOUNT_ID") or f"{provider}-1").strip()
    raw_secret = os.environ.get(f"{prefix}_SECRET_NAME")
    if raw_secret is not None:
        secret_name = raw_secret.strip()
    elif default_secret_name is None:
        secret_name = f"sbx-acct-{account_id}"
    else:
        secret_name = default_secret_name
    slots = env_int(f"{prefix}_SLOTS", default_slots)
    registry.put(
        Account(
            id=account_id,
            provider=provider,
            label=label,
            status="active",
            max_concurrent=slots,
            secret_name=secret_name,
            models=_models(provider),
            created_at=created_at,
        )
    )
    return account_id


def _seed_accounts(
    registry: InMemoryAccountRegistry,
    provider: str,
    *,
    default_secret_name: str | None,
    default_slots: int,
    label: str,
    created_at: str,
) -> list[Account]:
    """Seed ``provider``'s accounts; return them in seed order.

    ``SBX_<PROVIDER>_ACCOUNTS`` — a JSON list of ``{"id", "label"?,
    "secret_name"?, "slots"?, "models"?}`` — seeds a multi-account pool
    (SOR-63/D2: the Antigravity-4 / Grok-2 gate shape). Per entry,
    ``secret_name`` defaults to the ``sbx-acct-<id>`` convention (or
    ``default_secret_name`` when the provider overrides it), ``slots`` to
    ``default_slots``, ``models`` to the provider defaults. Without the
    JSON var, falls back to the single-account ``SBX_<PROVIDER>_ACCOUNT_ID``
    path.
    """
    prefix = f"SBX_{provider.upper()}"
    raw = os.environ.get(f"{prefix}_ACCOUNTS")
    if raw is None:
        account_id = _seed_account(
            registry,
            provider,
            default_secret_name=default_secret_name,
            default_slots=default_slots,
            label=label,
            created_at=created_at,
        )
        account = registry.get(account_id)
        assert account is not None
        return [account]
    try:
        specs = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{prefix}_ACCOUNTS is not valid JSON: {exc}") from exc
    if not isinstance(specs, list) or not specs:
        raise ValueError(f"{prefix}_ACCOUNTS must be a non-empty JSON list")
    seeded: list[Account] = []
    for index, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict) or not str(spec.get("id") or "").strip():
            raise ValueError(f"{prefix}_ACCOUNTS[{index - 1}] needs a non-empty 'id'")
        account_id = str(spec["id"]).strip()
        raw_secret = spec.get("secret_name")
        if raw_secret is not None:
            secret_name = str(raw_secret).strip()
        elif default_secret_name is None:
            secret_name = f"sbx-acct-{account_id}"
        else:
            secret_name = default_secret_name
        raw_models = spec.get("models")
        models = (
            tuple(str(m).strip() for m in raw_models if str(m).strip())
            if isinstance(raw_models, list)
            else _models(provider)
        )
        slots = spec.get("slots")
        account = Account(
            id=account_id,
            provider=provider,
            label=str(spec.get("label") or f"{label} {index}"),
            status="active",
            max_concurrent=int(slots) if slots is not None else default_slots,
            secret_name=secret_name,
            models=models,
            created_at=created_at,
        )
        registry.put(account)
        seeded.append(account)
    return seeded


def configure_v1_bootstrap(app: Any) -> bool:
    """Seed the P2 production gate from env; return whether it was enabled.

    The bearer plaintext is never attached to app state. Only its hash enters
    ``InMemoryApiKeyStore``. Account credentials stay in the per-account
    Modal Secrets referenced by name (``sbx-acct-<account_id>``); the seeded
    codex account carries no Secret name so sandboxes keep the default
    ``CODEX_AUTH_JSON`` credential path.
    """
    token = (os.environ.get("SBX_V1_BOOTSTRAP_KEY") or "").strip()
    if not token:
        return False

    key_store = InMemoryApiKeyStore()
    key_store.seed(token, label="p2.1-gate", scopes=("agents", "admin"))
    app.state.api_key_store = key_store

    registry = InMemoryAccountRegistry()
    created_at = datetime.now(UTC).isoformat()
    pools: dict[str, Any] = {}
    account_pools: dict[str, DevinAccountPool] = {}

    # Devin: the original P2.1 single account + tiered slot pool, unchanged.
    # SBX_DEVIN_ACCOUNTS switches it to a flat multi-account ProviderPool.
    devin_accounts = _seed_accounts(
        registry,
        "devin",
        default_secret_name=None,
        default_slots=env_int("SBX_DEVIN_BURST_SLOTS", 8),
        label="P2.1 Devin",
        created_at=created_at,
    )
    if len(devin_accounts) == 1:
        pools["devin"] = DevinAccountPool(registry, account_id=devin_accounts[0].id)
        account_pools[devin_accounts[0].id] = pools["devin"]
    else:
        member = {
            acct.id: SingleAccountPool(
                registry,
                provider="devin",
                account_id=acct.id,
                slots=acct.max_concurrent,
            )
            for acct in devin_accounts
        }
        pools["devin"] = ProviderPool(member)
        account_pools.update(member)

    # codex / antigravity / grok: one seeded account each behind a flat
    # single-account slot pool, or a ProviderPool when
    # ``SBX_<PROVIDER>_ACCOUNTS`` lists several. The codex account defaults
    # to an empty Secret name — its sandboxes keep the ``sbx-codex-auth`` /
    # ``CODEX_AUTH_JSON`` credential path (``SBX_CODEX_SECRET_NAME`` can name
    # a per-account Secret instead).
    for provider in ("codex", "antigravity", "grok"):
        accounts = _seed_accounts(
            registry,
            provider,
            default_secret_name="" if provider == "codex" else None,
            default_slots=env_int(f"SBX_{provider.upper()}_SLOTS", _DEFAULT_PROVIDER_SLOTS),
            label=f"P2 {provider}",
            created_at=created_at,
        )
        member = {
            acct.id: SingleAccountPool(
                registry,
                provider=provider,
                account_id=acct.id,
                slots=acct.max_concurrent,
            )
            for acct in accounts
        }
        pools[provider] = member[accounts[0].id] if len(accounts) == 1 else ProviderPool(member)
        account_pools.update(member)

    app.state.account_registry = registry
    app.state.scheduler = BootstrapScheduler(pools, account_pools=account_pools)
    return True
