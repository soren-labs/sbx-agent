"""Subscription provider id -> adapter. Codex is the only provider implemented."""

from __future__ import annotations

from control.integrations.subscriptions import codex
from control.integrations.subscriptions.base import SubscriptionAdapter

SUBSCRIPTIONS: dict[str, SubscriptionAdapter] = {codex.ADAPTER.provider_id: codex.ADAPTER}
