"""CLI entry: ``python -m runtime.runner``."""

from __future__ import annotations

import argparse
import sys

from runtime.runner.bootstrap import cmd_init
from runtime.runner.constants import DEFAULT_MAX_SECONDS, EXIT_INTERNAL
from runtime.runner.turn import cmd_stop, cmd_turn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Write CODEX_HOME config, auth, and session files")
    p_init.add_argument(
        "--auth",
        choices=["auth_json", "provider"],
        default="auth_json",
        help="Credential mode (default: auth_json)",
    )
    p_init.add_argument("--model", required=True, help="Codex model id")

    p_turn = sub.add_parser("turn", help="Run one Codex turn from a message file")
    p_turn.add_argument("--n", type=int, required=True, help="Turn number")
    p_turn.add_argument("--message-file", required=True, help="Path to the user message")
    p_turn.add_argument(
        "--max-seconds",
        type=int,
        default=DEFAULT_MAX_SECONDS,
        help="Soft timeout in seconds (default 900)",
    )

    sub.add_parser("stop", help="SIGTERM the current Codex pid, then SIGKILL after 30s")
    return parser


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "init":
        return cmd_init(auth=args.auth, model=args.model)
    if args.cmd == "turn":
        return cmd_turn(n=args.n, message_file=args.message_file, max_seconds=args.max_seconds)
    if args.cmd == "stop":
        return cmd_stop()
    return EXIT_INTERNAL


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(run(argv if argv is not None else sys.argv[1:]))
