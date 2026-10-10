"""Codex subscription adapter: official ``codex login --device-auth`` in a Setup VM."""

from __future__ import annotations

from control.integrations.subscriptions.base import PROFILE_MOUNT, SubscriptionAdapter

# File-backed auth keeps the login on the Slot Volume (a VM has no OS keyring).
_CODEX = ["codex", "-c", 'cli_auth_credentials_store="file"']
_WORK = "/tmp/sbx-setup/verify"

ADAPTER = SubscriptionAdapter(
    provider_id="codex",
    display_name="Codex",
    harness_provider_id="codex",
    login_method="device_code",
    verification_host="auth.openai.com",
    profile_env={
        "HOME": PROFILE_MOUNT,
        "CODEX_HOME": f"{PROFILE_MOUNT}/.codex",
        "XDG_CONFIG_HOME": f"{PROFILE_MOUNT}/.config",
        "XDG_DATA_HOME": f"{PROFILE_MOUNT}/.local/share",
    },
    setup={
        # The CLI does not create its home on a fresh Volume.
        "ensure_dirs": [f"{PROFILE_MOUNT}/.codex"],
        "login_argv": [*_CODEX, "login", "--device-auth"],
        "status_argv": [*_CODEX, "login", "status"],
        "status_ok": "Logged in using ChatGPT",
        "version_argv": ["codex", "--version"],
        # One real, tool-less model call proves the login is usable, not just present.
        "verify_argv": [
            *_CODEX,
            "--disable",
            "shell_tool",
            "exec",
            "--ephemeral",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-C",
            _WORK,
            "Do not use tools or read any files. Reply with exactly: SBX_SLOT_OK",
        ],
        "verify_ok": '"turn.completed"',
        "verify_cwd": _WORK,
        "url_pattern": r"https://auth\.openai\.com/\S+",
        "code_pattern": r"\b[A-Z0-9]{4,6}-[A-Z0-9]{4,6}\b",
        "expiry_pattern": r"(?:expires? in|valid for)\s+(\d+)\s+minutes?",
        "default_code_seconds": 900,
    },
)
