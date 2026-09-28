#!/usr/bin/env python3
"""Fake Antigravity CLI (``agy -p ... --output-format stream-json``).

Scenarios via ``FAKE_AGY_SCENARIO``: success, resume, nonzero, hang,
badjson, slow, auth_invalid. ``FAKE_AGY_SLOW_SECONDS`` overrides the
silent delay in ``slow`` (default 40). ``FAKE_AGY_SESSION_ID`` overrides
the emitted ``conversation_id``.

``FAKE_AGY_REQUIRE_ONBOARDING=1`` (SOR-258) makes ``agy models`` refuse a
restored credential that lacks the non-secret onboarding marker — the
real 1.2.x CLI's misleading "account not eligible" failure on a fresh
HOME that holds only the OAuth token.
"""

from __future__ import annotations

import os
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
ONBOARDING_STATE = (
    '{"consumerOnboardingComplete":true,"enterpriseOnboardingComplete":false,'
    '"onboardingComplete":true}\n'
)

_BOOL_FLAGS = {"-p", "--print", "--yolo", "--json"}
_VALUE_FLAGS = {"--output-format", "-m", "--model", "-C", "--cd", "--resume"}


def _rewrite_session(line: str, session_id: str) -> str:
    def _mutate(obj: dict) -> None:
        for key in ("init", "step_update", "result"):
            inner = obj.get(key)
            if isinstance(inner, dict) and "conversation_id" in inner:
                inner["conversation_id"] = session_id

    return rewrite_json(line, _mutate)


def _onboarding_gate(argv: list[str], onboarding: Path) -> None:
    """SOR-258: mimic the real CLI's fresh-HOME gate on ``agy models``.

    With ``FAKE_AGY_REQUIRE_ONBOARDING=1`` the probe refuses a restored
    credential that has no onboarding marker beside it — the real
    "account not eligible" failure token-only restores hit on 1.2.x.
    """
    if os.environ.get("FAKE_AGY_REQUIRE_ONBOARDING") != "1":
        return
    if tuple(argv[:1]) != ("models",):
        return
    if not _credential_ok(onboarding):
        print("error: account not eligible", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    install_term_handler()
    credential = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    onboarding = credential.parent / "cache" / "onboarding.json"
    _onboarding_gate(sys.argv[1:], onboarding)
    models_listing(sys.argv[1:], ("models",), credential, "FAKE_AGY_MODELS", DEFAULT_MODELS)
    login_flow(
        sys.argv[1:],
        (),
        credential,
        '{"token": "REDACTED"}\n',
        companions={onboarding: ONBOARDING_STATE},
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
