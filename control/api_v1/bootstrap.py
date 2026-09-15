"""Modal-only bootstrap wiring for the P2 real /v1 gate.

Nothing is enabled by default. When ``SBX_V1_BOOTSTRAP_KEY`` is injected via
a Modal Secret, seed a hash-only API key plus one account per P2 Core
provider — codex, devin, antigravity, grok — so the frozen candidate can
schedule all four through the product ``/v1`` path. Devin keeps its P2.1
tiered slot pool; the other providers get a flat single-account slot pool.
Full persistent multi-account storage remains SOR-63 scope; OpenCode stays
deferred and refuses with ``provider_exhausted``, not ``invalid_provider``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from control.api_v1.state import (
    PROVIDERS,
    InMemoryAccountRegistry,
    InMemoryApiKeyStore,
)
from control.config import env_int
from control.devin_pool import DevinAccountPool, ScheduleRefused, SlotLease
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
    ) -> None:
        super().__init__(
            registry,
            account_id=account_id,
            normal_slots=slots,
            soft_ceiling=slots,
            burst_slots=slots,
        )
        self.provider = provider


class BootstrapScheduler:
    """Frozen ``Scheduler`` surface + atomic ``acquire`` across provider pools.

    One seeded account per P2 Core provider, each behind a single-account
    slot pool. Contract-valid but unseeded providers (``opencode``) refuse
    ``auto`` picks with ``provider_exhausted`` and named picks with
    ``account_unavailable``; unknown ids get ``invalid_provider``.
    Multi-account LRU / failover is SOR-63 scope.
    """

    def __init__(self, pools: dict[str, DevinAccountPool]) -> None:
        self.pools = dict(pools)

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
    pools: dict[str, DevinAccountPool] = {}

    # Devin: the original P2.1 single account + tiered slot pool, unchanged.
    devin_id = _seed_account(
        registry,
        "devin",
        default_secret_name=None,
        default_slots=env_int("SBX_DEVIN_BURST_SLOTS", 8),
        label="P2.1 Devin",
        created_at=created_at,
    )
    pools["devin"] = DevinAccountPool(registry, account_id=devin_id)

    # codex / antigravity / grok: one seeded account each behind a flat
    # single-account slot pool. The codex account defaults to an empty
    # Secret name — its sandboxes keep the ``sbx-codex-auth`` /
    # ``CODEX_AUTH_JSON`` credential path (``SBX_CODEX_SECRET_NAME`` can name
    # a per-account Secret instead).
    for provider in ("codex", "antigravity", "grok"):
        account_id = _seed_account(
            registry,
            provider,
            default_secret_name="" if provider == "codex" else None,
            default_slots=_DEFAULT_PROVIDER_SLOTS,
            label=f"P2 {provider}",
            created_at=created_at,
        )
        pools[provider] = SingleAccountPool(
            registry,
            provider=provider,
            account_id=account_id,
            slots=env_int(f"SBX_{provider.upper()}_SLOTS", _DEFAULT_PROVIDER_SLOTS),
        )

    app.state.account_registry = registry
    app.state.scheduler = BootstrapScheduler(pools)
    return True
