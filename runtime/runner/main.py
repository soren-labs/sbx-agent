"""CLI entry: ``python -m runtime.runner``."""

from __future__ import annotations

import argparse
import sys

from runtime.runner.adapter import PROVIDERS
from runtime.runner.bootstrap import cmd_export_credentials, cmd_init
from runtime.runner.constants import DEFAULT_MAX_SECONDS, EXIT_INTERNAL
from runtime.runner.turn import cmd_stop, cmd_turn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Write provider config, credentials, and session files")
    p_init.add_argument(
        "--auth",
        choices=["auth_json", "provider"],
        default="auth_json",
        help="Credential mode (default: auth_json; codex only)",
    )
    p_init.add_argument("--model", required=True, help="Model id")
    p_init.add_argument(
        "--provider",
        choices=list(PROVIDERS),
        default="codex",
        help="Agent provider (default: codex)",
    )
    p_init.add_argument("--account-id", default=None, help="Account id for this session")
    p_init.add_argument(
        "--reasoning-effort",
        default=None,
        help="Canonical reasoning effort bound to every turn (SOR-179)",
    )

    p_turn = sub.add_parser("turn", help="Run one provider turn from a message file")
    p_turn.add_argument("--n", type=int, required=True, help="Turn number")
    p_turn.add_argument("--message-file", required=True, help="Path to the user message")
    p_turn.add_argument(
        "--max-seconds",
        type=int,
        default=DEFAULT_MAX_SECONDS,
        help="Soft timeout in seconds (default 900)",
    )
    p_turn.add_argument(
        "--output-contract",
        default=None,
        help="JSON file with a structured output contract "
        "({schema, enforcement}) — the turn's final message is "
        "extracted and validated against it (SOR-130)",
    )

    sub.add_parser("stop", help="SIGTERM the current provider pid, then SIGKILL after 30s")
    sub.add_parser(
        "export-credentials",
        help="Print the refreshed credential blob to stdout (empty when unchanged)",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "init":
        return cmd_init(
            auth=args.auth,
            model=args.model,
            provider=args.provider,
            account_id=args.account_id,
            reasoning_effort=args.reasoning_effort,
        )
    if args.cmd == "turn":
        return cmd_turn(
            n=args.n,
            message_file=args.message_file,
            max_seconds=args.max_seconds,
            output_contract=args.output_contract,
        )
    if args.cmd == "stop":
        return cmd_stop()
    if args.cmd == "export-credentials":
        return cmd_export_credentials()
    return EXIT_INTERNAL


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(run(argv if argv is not None else sys.argv[1:]))
