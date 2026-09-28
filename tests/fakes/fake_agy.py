#!/usr/bin/env python3
"""Fake Antigravity CLI (``agy -p ... --output-format stream-json``).

Scenarios via ``FAKE_AGY_SCENARIO``: success, resume, nonzero, hang,
badjson, slow, auth_invalid. ``FAKE_AGY_SLOW_SECONDS`` overrides the
silent delay in ``slow`` (default 40). ``FAKE_AGY_SESSION_ID`` overrides
the emitted ``conversation_id``.

SOR-258: the real CLI's eligibility gate also requires the onboarding
marker at ``~/.gemini/antigravity-cli/cache/onboarding.json`` — a restored
token without the completed state fails ``agy models`` with a misleading
"account not eligible". The fake models that gate so tests exercise the
real portable-credential contract; a fake login writes the completed
marker alongside the token.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from _fake_native import (
    _credential_ok,
    auth_check,
    install_term_handler,
    login_flow,
    models_listing,
    rewrite_json,
    run_scenario,
    scan_argv,
)

PROVIDER = "antigravity"
DEFAULT_SESSION_ID = "c3b66b04-872b-4fbe-a3a4-058a026ef20a"
DEFAULT_MODELS = "gemini-3.8-flash-low\ngemini-3.8-flash-high\ngemini-3.8-pro\n"
ONBOARDING_REL = ".gemini/antigravity-cli/cache/onboarding.json"

# The completed state a real OAuth onboarding writes (agy 1.2.x schema).
_COMPLETE_ONBOARDING = (
    '{\n  "consumerOnboardingComplete": true,\n'
    '  "enterpriseOnboardingComplete": true,\n'
    '  "onboardingComplete": true\n}\n'
)

_BOOL_FLAGS = {"-p", "--print", "--yolo", "--json"}
_VALUE_FLAGS = {"--output-format", "-m", "--model", "-C", "--cd", "--resume"}


def _onboarding_complete(home: Path) -> bool:
    """Mirror the real CLI's onboarding gate: marker present + complete."""
    path = home / ONBOARDING_REL
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(state, dict) and state.get("onboardingComplete") is True


def _rewrite_session(line: str, session_id: str) -> str:
    def _mutate(obj: dict) -> None:
        for key in ("init", "step_update", "result"):
            inner = obj.get(key)
            if isinstance(inner, dict) and "conversation_id" in inner:
                inner["conversation_id"] = session_id

    return rewrite_json(line, _mutate)


def main() -> None:
    install_term_handler()
    home = Path.home()
    credential = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    if sys.argv[1:] == ["models"] and _credential_ok(credential) and not _onboarding_complete(home):
        # The real eligibility failure seen on a token-only restore — the
        # account itself may be fine; the local onboarding state is what is
        # missing (SOR-258).
        print(
            "Eligibility check failed: Your current account is not eligible "
            "for Antigravity. Verify your account to continue.",
            file=sys.stderr,
        )
        sys.exit(1)
    models_listing(sys.argv[1:], ("models",), credential, "FAKE_AGY_MODELS", DEFAULT_MODELS)
    login_flow(
        sys.argv[1:],
        (),
        credential,
        '{"token": "REDACTED"}\n',
        extra_files={home / ONBOARDING_REL: _COMPLETE_ONBOARDING},
    )
    auth_check(sys.argv[1:], ("models",), credential)
    positionals, values, _seen = scan_argv(
        sys.argv[1:], bool_flags=_BOOL_FLAGS, value_flags=_VALUE_FLAGS
    )
    del positionals  # prompt text is ignored by the fake
    run_scenario(
        provider=PROVIDER,
        scenario_env="FAKE_AGY_SCENARIO",
        slow_env="FAKE_AGY_SLOW_SECONDS",
        session_env="FAKE_AGY_SESSION_ID",
        default_session_id=DEFAULT_SESSION_ID,
        is_resume="--resume" in values,
        session_id=values.get("--resume"),
        cwd=Path(values.get("-C") or values.get("--cd") or Path.cwd()),
        rewrite=_rewrite_session,
        hello_content="hello from fake_agy\n",
        resume_content="resumed by fake_agy\n",
    )


if __name__ == "__main__":
    main()
