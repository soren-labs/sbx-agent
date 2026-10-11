"""Subscription adapters are data for the Setup VM supervisor: official CLIs only, no secrets."""

from __future__ import annotations

import re

import pytest
from control.integrations.subscriptions.registry import SUBSCRIPTIONS
from protocol.capabilities import HARNESS_CLI_ARCHIVES, HARNESS_CLI_PACKAGES

BINARIES = {"codex": "codex", "antigravity": "agy", "grok": "grok"}


def test_registry_lists_the_three_providers() -> None:
    assert list(SUBSCRIPTIONS) == ["codex", "antigravity", "grok"]
    assert [a.display_name for a in SUBSCRIPTIONS.values()] == [
        "Codex",
        "Antigravity",
        "Grok Build",
    ]


@pytest.mark.parametrize("provider", sorted(BINARIES))
def test_setup_runs_only_the_pinned_official_cli_in_a_private_home(provider: str) -> None:
    adapter = SUBSCRIPTIONS[provider]
    setup = adapter.setup_spec("login")
    for key in ("login_argv", "status_argv", "version_argv", "verify_argv"):
        assert setup[key][0] == BINARIES[provider], key
    assert provider in HARNESS_CLI_PACKAGES or provider in HARNESS_CLI_ARCHIVES
    # Every path the CLI may write to is on the Slot Volume, never a shared home.
    assert adapter.profile_env["HOME"] == "/profile"
    assert all(
        value.startswith("/profile") for key, value in adapter.profile_env.items() if "HOME" in key
    )
    flat = repr(setup) + repr(adapter.profile_env)
    assert not re.search(r"api[_-]?key|secret|password|bearer", flat, re.I)
    assert adapter.login_method in ("device_code", "authorization_code")
    assert (adapter.login_method == "authorization_code") == ("pty" in setup)


def test_grok_patterns_match_the_cli_output_shape() -> None:
    setup = SUBSCRIPTIONS["grok"].setup_spec("login")
    printed = (
        "To sign in, open this URL in your browser:\n\n"
        "  https://accounts.x.ai/oauth2/device?user_code=AB12CD34\n\n"
        "Confirm this code in your browser:\n\n  AB12-CD34\n\nWaiting for authorization...\n"
    )
    url = re.search(setup["url_pattern"], printed)
    assert url and url.group(0) == "https://accounts.x.ai/oauth2/device?user_code=AB12CD34"
    assert re.search(setup["code_pattern"], printed).group(0) == "AB12-CD34"
    signed_out = "You are not authenticated.\n\nDefault model: m\n\nAvailable models:\n  * m"
    assert any(marker in signed_out.lower() for marker in setup["status_fail"])


def test_antigravity_sign_in_is_the_cli_screen_with_a_pasted_code() -> None:
    adapter = SUBSCRIPTIONS["antigravity"]
    setup = adapter.setup_spec("login")
    assert setup["login_argv"] == ["agy"], "the CLI has no login subcommand"
    assert setup["pty"]["steps"] == [{"expect": "Select login method", "send": "\r"}]
    screen = (
        "Open the URL below in your browser:\n"
        " https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=x&state=y\n"
        " After authenticating, copy the code displayed in the browser and paste it below:\n"
        " authorization code...\n"
    )
    assert re.search(setup["url_pattern"], screen).group(0).endswith("state=y")
    assert setup["pty"]["prompt"] in screen
    assert "please sign in" in setup["status_fail"]
    assert adapter.describe()["verification_host"] == "accounts.google.com"
