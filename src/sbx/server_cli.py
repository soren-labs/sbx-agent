"""Operator commands of the ``sbx`` CLI: ``serve``, ``migrate`` and ``sbx server ...``.

They need the control-plane package (``control``) and are imported only for these
commands, so client commands keep working from an SDK-only install.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def _problem(category: str, message: str, action: str | None) -> dict[str, Any]:
    found = {"name": "server", "category": category, "status": "fail", "message": message}
    if action:
        found["action"] = action
    return {"ok": False, "problem": found, "checks": [found]}


def run_operator(args: argparse.Namespace) -> int:
    from control.selfhost.checks import EXIT_CODES
    from control.selfhost.config import ConfigError

    try:
        return _dispatch(args)
    except ConfigError as exc:
        _print(_problem("config", exc.message, exc.action))
        return EXIT_CODES["config"]


def _settings(args: argparse.Namespace) -> Any:
    from control.selfhost.config import ServerSettings

    return ServerSettings.load(Path(args.config).expanduser() if args.config else None)


def _dispatch(args: argparse.Namespace) -> int:
    from control.selfhost import checks as chk
    from control.selfhost.process import ServerProcess

    if args.command in ("serve", "migrate"):
        return _foreground(args)
    action = args.action
    if action == "init":
        return _init(args)
    settings = _settings(args)
    if action == "machine":
        return _machine(args, settings)
    if action == "check":
        found = chk.run_all(settings, include_port=ServerProcess(settings).pid() is None)
        _print(chk.report(found, **settings.describe()))
        return chk.exit_code(found)
    process = ServerProcess(settings)
    if action == "logs":
        print("\n".join(process.tail(args.lines)))
        return 0
    if action == "outbox":
        return _outbox(settings.data_dir / "mail", args.to)
    result, code = {
        "start": lambda: process.start(timeout=args.timeout),
        "restart": lambda: process.restart(timeout=args.timeout),
        "stop": lambda: process.stop(keep_postgres=args.keep_postgres),
        "status": process.status,
    }[action]()
    _print(result)
    return code


def _init(args: argparse.Namespace) -> int:
    from control.selfhost.bootstrap import initialize
    from control.selfhost.config import ConfigError, default_config_path, default_data_dir

    database_url = None
    if args.external_database:
        if sys.stdin.isatty():
            import getpass

            database_url = getpass.getpass("PostgreSQL connection URL: ").strip()
        else:
            database_url = sys.stdin.readline().strip()
        if not database_url:
            raise ConfigError(
                "no PostgreSQL connection URL was given on stdin",
                "pipe it in, e.g. `sbx server init --external-database < dsn.txt`",
            )
    path = Path(args.config).expanduser() if args.config else default_config_path()
    _print(
        initialize(
            path,
            data_dir=Path(args.data_dir) if args.data_dir else default_data_dir(),
            database_url=database_url,
            host=args.host,
            port=args.port,
            public_url=args.public_url,
            executors=args.executors,
        )
    )
    return 0


def _foreground(args: argparse.Namespace) -> int:
    """``sbx serve`` / ``sbx migrate``: the config file (if any) under the environment."""
    from control.composition import main as control_main
    from control.selfhost.postgres import ManagedPostgres

    settings = _settings(args)
    if settings.file_exists:
        os.environ.update(settings.values)
        if settings.managed_postgres:
            ManagedPostgres(settings.data_dir).start()
    argv = [args.command]
    if args.command == "serve":
        host = args.host or settings.host
        port = args.port or settings.port
        argv += ["--host", host, "--port", str(port)]
    return control_main(argv)


def _outbox(directory: Path, to: str | None) -> int:
    from control.selfhost.checks import EXIT_CODES

    messages = sorted(directory.glob("*.json"), reverse=True) if directory.is_dir() else []
    for path in messages:
        data = json.loads(path.read_text())
        if to is None or data.get("to") == to.strip().lower():
            _print({**data, "file": str(path)})
            return 0
    _print(
        _problem(
            "config",
            f"no local email{' for ' + to if to else ''} in {directory}",
            "register first; with SBX_RESEND_API_KEY set, email is sent by Resend instead",
        )
    )
    return EXIT_CODES["config"]


def _machine(args: argparse.Namespace, settings: Any) -> int:
    from control.selfhost.checks import EXIT_CODES
    from control.selfhost.machines import MachineError, run

    try:
        _print(run(args, settings))
        return 0
    except MachineError as exc:
        out = _problem(exc.category, exc.message, exc.action)
        out.update(exc.extra)
        _print(out)
        return EXIT_CODES.get(exc.category, 3)
