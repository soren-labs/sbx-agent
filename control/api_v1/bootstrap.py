"""Modal-only bootstrap wiring for the P2.1 real /v1 gate.

Nothing is enabled by default. When ``SBX_V1_BOOTSTRAP_KEY`` is injected via
a Modal Secret, seed a hash-only API key plus the single Devin account/pool
needed for P2.1. Full persistent multi-account storage remains SOR-63 scope.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from control.api_v1.state import InMemoryAccountRegistry, InMemoryApiKeyStore
from control.config import env_int
from control.devin_pool import DevinAccountPool
from control.ports import Account


def _models() -> tuple[str, ...]:
    raw = os.environ.get("SBX_DEVIN_MODELS", "swe-2-high,swe-2-medium")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def configure_v1_bootstrap(app: Any) -> bool:
    """Seed the P2.1 production gate from env; return whether it was enabled.

    The bearer plaintext is never attached to app state. Only its hash enters
    ``InMemoryApiKeyStore``. Devin credentials remain in the separate Modal
    account Secret referenced by name.
    """
    token = (os.environ.get("SBX_V1_BOOTSTRAP_KEY") or "").strip()
    if not token:
        return False

    key_store = InMemoryApiKeyStore()
    key_store.seed(token, label="p2.1-gate", scopes=("agents", "admin"))
    app.state.api_key_store = key_store

    account_id = (os.environ.get("SBX_DEVIN_ACCOUNT_ID") or "devin-1").strip()
    secret_name = (os.environ.get("SBX_DEVIN_SECRET_NAME") or f"sbx-acct-{account_id}").strip()
    registry = InMemoryAccountRegistry()
    registry.put(
        Account(
            id=account_id,
            provider="devin",
            label="P2.1 Devin",
            status="active",
            max_concurrent=env_int("SBX_DEVIN_BURST_SLOTS", 8),
            secret_name=secret_name,
            models=_models(),
            created_at=datetime.now(UTC).isoformat(),
        )
    )
    app.state.account_registry = registry
    app.state.scheduler = DevinAccountPool(registry, account_id=account_id)
    return True
