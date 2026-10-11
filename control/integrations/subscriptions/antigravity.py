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

import re
from typing import Any

from control.integrations.subscriptions.base import PROFILE_MOUNT, SubscriptionAdapter

_WORK = "/tmp/sbx-setup/verify"
# A model slug as ``--model`` takes it: lower-case, no spaces.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._:/-]{1,79}$")
_NOISE = ("fetching", "error", "warning", "available models", "models:")


def parse_catalog(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Model slugs from the lines ``agy models`` printed; a line that is not a slug is ignored.

    ``agy`` has no machine-readable catalog. Its ``--effort`` levels belong to the CLI, not
    to a model (the CLI resolves a level per model), so every model carries the same levels
    and the catalog says so with ``effort_scope="cli"``. Nothing is added to what was printed.
    """
    levels = [
        {"id": level, "description": None}
        for level in dict.fromkeys(str(raw.get("efforts") or "").split("|"))
        if re.fullmatch(r"[a-z]{2,12}", level)
    ]
    models: list[dict[str, Any]] = []
    for line in raw.get("lines") or []:
        if not isinstance(line, str) or line.lower().startswith(_NOISE):
            continue
        marked = line.startswith(("*", ">")) or "(default)" in line.lower()
        tokens = line.lstrip("*>-•· \t").split()
        slug = tokens[0].strip("`\"',") if tokens else ""
        if not _SLUG.match(slug) or slug in [m["id"] for m in models]:
            continue
        models.append(
            {
                "id": slug,
                "name": slug,
                "description": None,
                "default": marked,
                "reasoning": {
                    "kind": "levels" if levels else "none",
                    "efforts": levels,
                    "default": None,
                },
            }
        )
    if not models:
        return None
    defaults = [m["id"] for m in models if m["default"]]
    return {
        "models": models,
        "default_model": defaults[0] if len(defaults) == 1 else None,
        "complete": bool(raw.get("complete")),
        "effort_scope": "cli" if levels else None,
    }


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
    catalog_source="agy models",
    parse_catalog=parse_catalog,
    setup={
        "catalog": {
            "kind": "text",
            "argv": ["agy", "models"],
            # The effort levels the installed CLI accepts, from its own usage text.
            "facts": {
                "efforts": {"argv": ["agy", "--help"], "pattern": r"--effort\s[^\n]*\(([a-z|]+)\)"}
            },
            "timeout": 60,
        },
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
