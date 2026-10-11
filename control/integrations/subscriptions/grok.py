"""Grok Build subscription adapter: official ``grok login --device-auth`` in a Setup VM.

Verified against Grok Build 1.0.50: the device login prints a ``accounts.x.ai`` URL and a
one-time code and stores the session under ``$GROK_HOME`` (file-backed, no OS keyring).
The CLI has no login-status command; ``grok models`` says ``You are not authenticated``
while signed out and exits 0, so its words decide.
"""

from __future__ import annotations

from control.integrations.subscriptions.base import PROFILE_MOUNT, SubscriptionAdapter

# Never self-update inside a VM: the image pins the verified version.
_GROK = ["grok", "--no-auto-update"]
_WORK = "/tmp/sbx-setup/verify"

ADAPTER = SubscriptionAdapter(
    provider_id="grok",
    display_name="Grok Build",
    harness_provider_id="grok",
    login_method="device_code",
    verification_host="accounts.x.ai",
    profile_env={
        "HOME": PROFILE_MOUNT,
        "GROK_HOME": f"{PROFILE_MOUNT}/.grok",
        "XDG_CONFIG_HOME": f"{PROFILE_MOUNT}/.config",
        "XDG_DATA_HOME": f"{PROFILE_MOUNT}/.local/share",
    },
    setup={
        "ensure_dirs": [f"{PROFILE_MOUNT}/.grok"],
        "login_argv": ["grok", "login", "--device-auth"],
        "status_argv": [*_GROK, "models"],
        "status_ok": "Available models",
        "status_fail": ["not authenticated", "not signed in"],
        "version_argv": ["grok", "--version"],
        # One real, tool-less model call proves the login is usable, not just present.
        "verify_argv": [
            *_GROK,
            "-p",
            "Do not use tools or read any files. Reply with exactly: SBX_SLOT_OK",
            "--output-format",
            "json",
            "--max-turns",
            "1",
            "--disable-web-search",
            "--cwd",
            _WORK,
        ],
        "verify_ok": '"stopReason"',
        "verify_cwd": _WORK,
        "url_pattern": r"https://accounts\.x\.ai/\S+",
        "code_pattern": r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b",
        "expiry_pattern": r"(?:expires? in|valid for)\s+(\d+)\s+minutes?",
        "default_code_seconds": 900,
    },
)
