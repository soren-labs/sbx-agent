#!/usr/bin/env python3
"""Fake OpenCode CLI (``opencode run --session <id>``).

Scenarios via ``FAKE_OPENCODE_SCENARIO``: success, resume, nonzero, hang,
badjson, slow, auth_invalid. ``FAKE_OPENCODE_SLOW_SECONDS`` overrides the
silent delay in ``slow`` (default 40). ``FAKE_OPENCODE_SESSION_ID``
overrides the emitted ``sessionID``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from _fake_native import (
    install_term_handler,
    rewrite_json,
    run_scenario,
    scan_argv,
)

PROVIDER = "opencode"
DEFAULT_SESSION_ID = "ses_01a09b11abcd"

_BOOL_FLAGS = {"--print-logs", "--continue", "--last", "-c"}
_VALUE_FLAGS = {"-m", "--model", "-C", "--cd", "-s", "--session"}


def _rewrite_session(line: str, session_id: str) -> str:
    def _mutate(obj: dict) -> None:
        if "sessionID" in obj:
            obj["sessionID"] = session_id
        part = obj.get("part")
        if isinstance(part, dict) and "sessionID" in part:
            part["sessionID"] = session_id

    return rewrite_json(line, _mutate)


def main() -> None:
    install_term_handler()
    positionals, values, seen = scan_argv(
        sys.argv[1:],
        bool_flags=_BOOL_FLAGS,
        value_flags=_VALUE_FLAGS,
        subcommand="run",
    )
    del positionals
    session_id = values.get("--session") or values.get("-s")
    run_scenario(
        provider=PROVIDER,
        scenario_env="FAKE_OPENCODE_SCENARIO",
        slow_env="FAKE_OPENCODE_SLOW_SECONDS",
        session_env="FAKE_OPENCODE_SESSION_ID",
        default_session_id=DEFAULT_SESSION_ID,
        is_resume=session_id is not None or "--continue" in seen or "--last" in seen,
        session_id=session_id,
        cwd=Path(values.get("-C") or values.get("--cd") or Path.cwd()),
        rewrite=_rewrite_session,
        hello_content="hello from fake_opencode\n",
        resume_content="resumed by fake_opencode\n",
    )


if __name__ == "__main__":
    main()
