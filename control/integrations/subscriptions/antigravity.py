"""Antigravity subscription adapter: the official ``agy`` remote sign-in in a Setup VM.

Verified against Antigravity CLI 1.3.3. ``agy`` has no login subcommand: its first-run
screen offers "Google OAuth", prints a ``accounts.google.com`` URL and asks for the
authorization code the browser shows afterwards (the documented remote/SSH sign-in). The
Setup VM runs that screen under a PTY, relays the URL, and types the code the user
submits. With no D-Bus session bus the CLI keeps the login in files under ``HOME``, which
is the Slot Volume.

There is no login-status command; ``agy models`` answers ``Please sign in`` (exit 0)
while signed out, so its words decide.
"""

from __future__ import annotations

from control.integrations.subscriptions.base import PROFILE_MOUNT, SubscriptionAdapter

_WORK = "/tmp/sbx-setup/verify"

ADAPTER = SubscriptionAdapter(
    provider_id="antigravity",
    display_name="Antigravity",
    harness_provider_id="antigravity",
    login_method="authorization_code",
    verification_host="accounts.google.com",
    profile_env={
        "HOME": PROFILE_MOUNT,
        "XDG_CONFIG_HOME": f"{PROFILE_MOUNT}/.config",
        "XDG_DATA_HOME": f"{PROFILE_MOUNT}/.local/share",
        "XDG_CACHE_HOME": f"{PROFILE_MOUNT}/.cache",
        "TERM": "xterm-256color",
        # The image pins the verified version; the header must not show the account.
        "AGY_CLI_DISABLE_AUTO_UPDATE": "1",
        "AGY_CLI_HIDE_ACCOUNT_INFO": "1",
    },
    setup={
        "ensure_dirs": [f"{PROFILE_MOUNT}/.gemini"],
        "login_argv": ["agy"],
        "pty": {
            # The first-run screen preselects "Google OAuth"; Enter confirms it.
            "steps": [{"expect": "Select login method", "send": "\r"}],
            "prompt": "authorization code",
            "input": "authorization_code",
        },
        "status_argv": ["agy", "models"],
        "status_ok": "",
        "status_fail": ["please sign in", "not logged in", "authentication required"],
        "status_interval": 5,
        "version_argv": ["agy", "--version"],
        # One real model call proves the login is usable. Print mode has no prompts, so a
        # tool that needs approval is soft-denied rather than run.
        "verify_argv": [
            "agy",
            "-p",
            "Do not use tools or read any files. Reply with exactly: SBX_SLOT_OK",
            "--output-format",
            "json",
            "--print-timeout",
            "150s",
        ],
        "verify_ok": '"SUCCESS"',
        "verify_cwd": _WORK,
        "url_pattern": r"https://accounts\.google\.com/o/oauth2/\S+",
        "default_code_seconds": 900,
    },
)
