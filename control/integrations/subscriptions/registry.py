"""Subscription provider id -> adapter."""

from __future__ import annotations

from control.integrations.subscriptions import antigravity, codex, grok
from control.integrations.subscriptions.base import SubscriptionAdapter

SUBSCRIPTIONS: dict[str, SubscriptionAdapter] = {
    adapter.provider_id: adapter for adapter in (codex.ADAPTER, antigravity.ADAPTER, grok.ADAPTER)
}
