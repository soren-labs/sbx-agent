"""Provider-neutral contract for subscription Machine Slots.

An adapter is data plus small pure functions: it tells the Setup VM supervisor how to
drive the provider's official CLI and tells the executor how the private profile Volume
is mounted. It never handles credentials; those stay inside the VM on the Slot Volume.
"""

from __future__ import annotations

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
        }
