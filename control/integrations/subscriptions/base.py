"""Provider-neutral contract for subscription Machine Slots.

An adapter is data plus small pure functions: it tells the Setup VM supervisor how to
drive the provider's official CLI and tells the executor how the private profile Volume
is mounted. It never handles credentials; those stay inside the VM on the Slot Volume.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

PROFILE_MOUNT = "/profile"


@dataclass(frozen=True)
class SubscriptionAdapter:
    provider_id: str
    display_name: str
    # The Harness that executes Turns for this subscription.
    harness_provider_id: str
    login_method: str
    # Official page where the user approves the device code (shown before the CLI reports it).
    verification_host: str
    profile_env: dict[str, str] = field(default_factory=dict)
    setup: dict[str, Any] = field(default_factory=dict)
    login_window_seconds: int = 900
    # Where the model catalog comes from (shown to users), and how its raw items normalize.
    catalog_source: str | None = None
    parse_catalog: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None

    def setup_spec(self, mode: str) -> dict[str, Any]:
        """Supervisor input for ``runtime.subscriptions.setup`` (``login`` or ``verify``)."""
        return {
            **self.setup,
            "mode": mode,
            "profile_dir": PROFILE_MOUNT,
            "deadline_seconds": self.login_window_seconds,
        }

    def describe(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "harness_provider_id": self.harness_provider_id,
            "login_method": self.login_method,
            "verification_host": self.verification_host,
            "login_window_seconds": self.login_window_seconds,
            "catalog_source": self.catalog_source,
        }

    def catalog(self, raw: Any) -> dict[str, Any] | None:
        """Normalized catalog ``{models, default_model, complete}`` or ``None`` (untrusted input)."""
        if self.parse_catalog is None or not isinstance(raw, dict):
            return None
        return self.parse_catalog(raw)
