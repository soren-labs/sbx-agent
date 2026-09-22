"""``sbx`` CLI dispatch (SOR-98).

Stable entrypoints: ``init``, ``credentials``, ``config``, ``status``,
``deploy``, ``doctor``, ``smoke``, ``upgrade``, ``uninstall``. ``python -m
sbx`` and the ``sbx`` console script both land here. ``main()`` accepts
``plane``/``transport``/``auth_check`` injection so the whole surface is
testable without cloud credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from sbx.config import CONFIG_ENV, STATE_DIR_ENV, ResolvedConfig, load
from sbx.errors import BootstrapError
from sbx.plane import ModalPlane, Plane


def _env_with(args: argparse.Namespace) -> dict[str, str]:
    env = dict(os.environ)
    if getattr(args, "config", None):
        env[CONFIG_ENV] = args.config
    if getattr(args, "state_dir", None):
        env[STATE_DIR_ENV] = args.state_dir
    return env


def _resolve(args: argparse.Namespace, env: Mapping[str, str]) -> ResolvedConfig:
    return load(env=env)


def _plane(cfg: ResolvedConfig, args: argparse.Namespace) -> Plane:
    return args.plane or ModalPlane(profile=cfg.config.modal_profile)


def _print_check(check: Any) -> None:
    label = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}[check.status]
    print(f"{label} {check.name} — {check.detail}")
    if check.hint and not check.ok:
        print(f"     hint: {check.hint}")
    elif check.hint:
        print(f"     next: {check.hint}")


def _emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2))


def _providers_arg(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    return tuple(p.strip() for p in value.split(",") if p.strip())


# --------------------------------------------------------------------- init


def _scan_payload(scan: Any) -> dict[str, Any]:
    return {
        "provider": scan.provider,
        "status": scan.status,
        "path": scan.path,
        "detail": scan.detail,
        "hint": scan.hint,
        "login": scan.login,
    }


def cmd_init(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from sbx.init import init

    cfg = _resolve(args, env)
    report = init(
        cfg,
        _plane(cfg, args),
        env=env,
        profile=args.profile,
        app_name=args.app_name,
        base_url=args.base_url,
        providers=_providers_arg(args.providers),
        github=args.github,
        github_secret=args.github_secret,
        verify=args.verify,
        allow_open_permissions=args.allow_open_permissions,
        auth_check=args.auth_check,
    )
    if args.json:
        _emit_json(
            {
                "ok": True,
                "config_path": str(report.config_path),
                "config_created": report.config_created,
                "state_dir": str(report.state_dir),
                "checks": [
                    {"name": c.name, "status": c.status, "detail": c.detail, "hint": c.hint}
                    for c in report.checks
                ],
                "credentials": [_scan_payload(s) for s in report.credentials],
            }
        )
        return 0
    for check in report.checks:
        _print_check(check)
    verb = "created" if report.config_created else "updated"
    print(f"config {verb}: {report.config_path}")
    if not report.authenticated:
        print("next: authenticate Modal (`modal token new`), then `sbx deploy`")
    elif any(not s.ok for s in report.credentials):
        missing = ", ".join(s.provider for s in report.credentials if not s.ok)
        print(f"next: fix provider credentials ({missing}) — see hints — then `sbx deploy`")
    else:
        print("next: `sbx deploy`")
    return 0


# --------------------------------------------------------------- credentials


def cmd_credentials(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from functools import partial

    from sbx.credentials import cli_auth_check, scan_credentials

    cfg = _resolve(args, env)
    providers = _providers_arg(args.providers)
    selected = providers if providers is not None else cfg.config.providers
    auth_check = args.auth_check or (partial(cli_auth_check, env=env) if args.verify else None)
    scans = scan_credentials(
        selected,
        env=env,
        allow_open_permissions=args.allow_open_permissions,
        auth_check=auth_check,
    )
    if args.json:
        _emit_json({"credentials": [_scan_payload(s) for s in scans]})
        return 0
    if not scans:
        print("no providers selected — set deploy.providers or pass --providers")
        return 0
    for scan in scans:
        _print_check(scan.to_check())
    blocked = [s for s in scans if not s.ok]
    if blocked:
        print("next: follow the hints above, then rerun `sbx credentials --verify`")
    else:
        print("next: import the credential(s) as hinted, then `sbx deploy`")
    return 0


# --------------------------------------------------------------- config/show


def cmd_config(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    cfg = _resolve(args, env)
    config = cfg.config
    rows = [
        (name, getattr(config, name), cfg.sources.get(name, "default"))
        for name in config.__dataclass_fields__
    ]
    if args.json:
        _emit_json(
            {
                "path": str(cfg.path),
                "file_exists": cfg.file_exists,
                "values": {
                    name: {"value": value, "source": source} for name, value, source in rows
                },
            }
        )
        return 0
    print(f"config: {cfg.path} ({'file' if cfg.file_exists else 'defaults only'})")
    for name, value, source in rows:
        rendered = (
            ",".join(value)
            if isinstance(value, tuple)
            else ((str(value) or "-") if value is not None else "-")
        )
        print(f"  {name} = {rendered}   ({source})")
    return 0


def cmd_status(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from sbx.deploy import read_deploy_state
    from sbx.doctor import live_agent_count, provider_summaries
    from sbx.httpapi import V1Client
    from sbx.keys import fingerprint, resolve_api_key

    cfg = _resolve(args, env)
    state = read_deploy_state(env)
    token = resolve_api_key(env)
    base_url = cfg.config.api_base_url or state.get("app_url") or ""
    providers: Any = "unknown"
    live_agents: int | None = None
    if base_url and token:
        try:
            with V1Client(base_url, token, transport=args.transport, timeout=10.0) as client:
                models = client.models().get("models", [])
                providers = provider_summaries(models)
                try:
                    live_agents = live_agent_count(client)
                except Exception:
                    live_agents = None
        except Exception:
            providers = "unreachable"
    cap = cfg.config.max_concurrent
    github_secret = cfg.config.github_secret_name or None
    payload = {
        "config_path": str(cfg.path),
        "config_exists": cfg.file_exists,
        "app": cfg.config.modal_app_name,
        "base_url": base_url,
        "deployed_version": state.get("version"),
        "deployed_at": state.get("deployed_at"),
        "key_fingerprint": fingerprint(token) if token else None,
        "providers": providers,
        "live_agents": live_agents,
        "concurrency_cap": cap,
        "github_bridge": {
            "enabled": cfg.config.github_ephemeral,
            "secret_name": github_secret,
        },
    }
    if args.json:
        _emit_json(payload)
        return 0
    print(f"config:    {payload['config_path']} ({'file' if cfg.file_exists else 'defaults'})")
    print(f"app:       {payload['app']}")
    print(f"base:      {base_url or '-'}")
    print(f"deploy:    {state.get('version') or 'never'} at {state.get('deployed_at') or '-'}")
    print(f"key:       {payload['key_fingerprint'] or 'none (run `sbx deploy`)'}")
    rendered = ", ".join(providers) if isinstance(providers, list) else providers
    print(f"providers: {rendered or '-'}")
    if cfg.config.github_ephemeral:
        gh_line = (
            f"armed (Modal Secret {github_secret})"
            if github_secret
            else "armed (env GH_TOKEN/GITHUB_TOKEN)"
        )
    else:
        gh_line = "off"
    print(f"github:    {gh_line}")
    if live_agents is None:
        agents_line = "-"
    elif cap is not None:
        agents_line = f"{live_agents} live / cap {cap} (SBX_MAX_CONCURRENT)"
    else:
        agents_line = f"{live_agents} live (SBX_MAX_CONCURRENT unset — remote defaults apply)"
    print(f"agents:    {agents_line}")
    return 0


# -------------------------------------------------------------------- deploy


def _print_deploy(report: Any, env: Mapping[str, str]) -> None:
    from sbx.config import key_path

    for step in report.steps:
        mark = "*" if step.changed else "="
        print(f"{mark} {step.name}: {step.detail}")
    print(f"deployed {report.version} → {report.base_url}")
    if report.key_created:
        print(f"bootstrap key minted — saved to {key_path(env)} (mode 0600)")
    if report.key_rotated:
        print("bootstrap key rotated — remote secret now matches the new local key")
    print("use with the client:")
    print(f"  export SBX_BASE_URL={report.base_url}")
    print(f"  export SBX_API_KEY=$(cat {key_path(env)})")


def cmd_deploy(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from sbx.deploy import deploy

    cfg = _resolve(args, env)
    report = deploy(
        cfg,
        _plane(cfg, args),
        env=env,
        transport=args.transport,
        sleep=args.sleep,
        probe_attempts=args.probe_attempts,
        versions_lock=args.versions_lock,
    )
    if args.json:
        _emit_json(
            {
                "ok": True,
                "base_url": report.base_url,
                "version": report.version,
                "cli_versions": report.cli_versions or {},
                "steps": [
                    {"name": s.name, "changed": s.changed, "detail": s.detail} for s in report.steps
                ],
            }
        )
    else:
        _print_deploy(report, env)
    return 0


# -------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from sbx.doctor import failed, run_doctor

    cfg = _resolve(args, env)
    checks = run_doctor(
        cfg,
        _plane(cfg, args),
        env=env,
        transport=args.transport,
        verify=args.verify,
        allow_open_permissions=args.allow_open_permissions,
        auth_check=args.auth_check,
    )
    bad = failed(checks)
    if args.json:
        _emit_json(
            {
                "ok": not bad,
                "checks": [
                    {"name": c.name, "status": c.status, "detail": c.detail, "hint": c.hint}
                    for c in checks
                ],
            }
        )
    else:
        for check in checks:
            _print_check(check)
    if bad:
        raise BootstrapError(
            f"{len(bad)} required check(s) failed: {', '.join(c.name for c in bad)}",
            hint=bad[0].hint,
            code="doctor_failed",
        )
    return 0


# --------------------------------------------------------------------- smoke


def cmd_smoke(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from sbx.smoke import run_smoke

    cfg = _resolve(args, env)
    result = run_smoke(
        cfg,
        env=env,
        transport=args.transport,
        provider=args.provider,
        prompt=args.prompt,
        timeout_s=args.timeout,
        poll_s=args.poll,
        sleep=args.sleep,
    )
    if args.json:
        _emit_json(
            {
                "ok": True,
                "agent_id": result.agent_id,
                "run_id": result.run_id,
                "status": result.status,
                "elapsed_s": result.elapsed_s,
            }
        )
    else:
        print(f"smoke ok — run {result.run_id} FINISHED in {result.elapsed_s}s")
    return 0


# ------------------------------------------------------------------- upgrade


def cmd_upgrade(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from sbx.deploy import upgrade

    cfg = _resolve(args, env)
    report = upgrade(
        cfg,
        _plane(cfg, args),
        env=env,
        transport=args.transport,
        sleep=args.sleep,
        probe_attempts=args.probe_attempts,
        version=args.version,
        versions_lock=args.versions_lock,
    )
    if args.json:
        _emit_json(
            {
                "ok": True,
                "from_version": report.from_version,
                "to_version": report.to_version,
                "base_url": report.deploy.base_url,
                "durable": report.durable,
            }
        )
    else:
        print(f"upgrade {report.from_version} → {report.to_version}")
        for name, count in report.durable.items():
            print(f"  durable {name}: {count} key(s) preserved")
        _print_deploy(report.deploy, env)
    return 0


# ----------------------------------------------------------------- uninstall


def cmd_uninstall(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from sbx.uninstall import uninstall

    cfg = _resolve(args, env)
    report = uninstall(
        cfg,
        _plane(cfg, args),
        env=env,
        purge_data=args.purge_data,
        purge_credentials=args.purge_credentials,
    )
    if args.json:
        _emit_json(
            {
                "ok": True,
                "terminated_sandboxes": list(report.terminated_sandboxes),
                "app_stopped": report.app_stopped,
                "deleted_dicts": list(report.deleted_dicts),
                "deleted_secrets": list(report.deleted_secrets),
                "removed_local_files": list(report.removed_local_files),
                "preserved": list(report.preserved),
            }
        )
        return 0
    print(f"terminated {len(report.terminated_sandboxes)} sandbox(es)")
    print(f"app {'stopped' if report.app_stopped else 'was not running'}")
    if report.deleted_dicts:
        print(f"deleted dicts: {', '.join(report.deleted_dicts)}")
    if report.deleted_secrets:
        print(f"deleted secrets: {', '.join(report.deleted_secrets)}")
    if report.removed_local_files:
        print(f"removed local files: {', '.join(report.removed_local_files)}")
    if report.preserved:
        print(f"preserved: {', '.join(report.preserved)}")
    return 0


# --------------------------------------------------------------------- parser


def _common(default: Any) -> argparse.ArgumentParser:
    """Shared flags. ``default`` is None on the top parser and
    ``argparse.SUPPRESS`` on subparsers so unset flags never clobber
    values already parsed at the top level."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", default=default, help="config file path (default: $SBX_CONFIG or XDG)"
    )
    common.add_argument(
        "--state-dir",
        dest="state_dir",
        default=default,
        help="state dir (default: $SBX_STATE_DIR or XDG)",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=default if default is argparse.SUPPRESS else False,
        help="machine-readable output",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    top = _common(None)
    sub_common = _common(argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        prog="sbx",
        description="Deploy and operate the sbx control plane on your own Modal workspace.",
        parents=[top],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", parents=[sub_common], help="check prerequisites and write config")
    p.add_argument("--profile", help="Modal profile name")
    p.add_argument("--app-name", help="Modal app name")
    p.add_argument("--base-url", help="public API base URL")
    p.add_argument("--providers", help="comma-separated providers to build images for")
    p.add_argument(
        "--verify",
        action="store_true",
        help="also run each provider CLI's own auth check on discovered credentials",
    )
    p.add_argument(
        "--allow-open-permissions",
        action="store_true",
        help="accept credential files readable by group/other",
    )
    p.add_argument(
        "--github",
        action=argparse.BooleanOptionalAction,
        default=argparse.SUPPRESS,
        help="persist the GitHub auth bridge gate in config (github.ephemeral)",
    )
    p.add_argument(
        "--github-secret",
        dest="github_secret",
        default=argparse.SUPPRESS,
        metavar="NAME",
        help="persist the Modal Secret name holding GH_TOKEN for the GitHub "
        "bridge (github.secret_name; the token itself is never stored)",
    )
    p.set_defaults(func=cmd_init)

    p = sub.add_parser(
        "credentials",
        parents=[sub_common],
        help="scan selected providers' local credential files (never prints contents)",
    )
    p.add_argument("--providers", help="comma-separated providers to scan (default: configured)")
    p.add_argument(
        "--verify",
        action="store_true",
        help="also run each provider CLI's own auth check on discovered credentials",
    )
    p.add_argument(
        "--allow-open-permissions",
        action="store_true",
        help="accept credential files readable by group/other",
    )
    p.set_defaults(func=cmd_credentials)

    p = sub.add_parser("config", parents=[sub_common], help="show resolved non-sensitive config")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("status", parents=[sub_common], help="show deployment status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "deploy", parents=[sub_common], help="idempotent deploy of the control plane"
    )
    p.add_argument(
        "--versions-lock",
        metavar="PATH",
        help="replay a frozen CLI versions lock file (rollback to an earlier "
        "deployment's provider CLI versions; SOR-175)",
    )
    p.set_defaults(func=cmd_deploy, probe_attempts=5)

    p = sub.add_parser("doctor", parents=[sub_common], help="verify the deployment end to end")
    p.add_argument(
        "--verify",
        action="store_true",
        help="also run each provider CLI's own auth check on discovered credentials",
    )
    p.add_argument(
        "--allow-open-permissions",
        action="store_true",
        help="accept credential files readable by group/other",
    )
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("smoke", parents=[sub_common], help="run a minimal /v1 agent to terminal")
    p.add_argument("--provider", help="provider to smoke (default: first configured)")
    p.add_argument("--prompt", default=None, help="smoke prompt text")
    p.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for terminal")
    p.add_argument("--poll", type=float, default=3.0, help="poll interval seconds")
    p.set_defaults(func=cmd_smoke)

    p = sub.add_parser("upgrade", parents=[sub_common], help="redeploy preserving durable stores")
    p.add_argument("--version", help="version string to record (default: package version)")
    p.add_argument(
        "--versions-lock",
        metavar="PATH",
        help="replay a frozen CLI versions lock file (rollback to an earlier "
        "deployment's provider CLI versions; SOR-175)",
    )
    p.set_defaults(func=cmd_upgrade, probe_attempts=5)

    p = sub.add_parser(
        "uninstall", parents=[sub_common], help="stop app and terminate sbx sandboxes"
    )
    p.add_argument("--purge-data", action="store_true", help="also delete durable Dicts")
    p.add_argument(
        "--purge-credentials",
        action="store_true",
        help="also delete Modal Secrets and the local key file",
    )
    p.set_defaults(func=cmd_uninstall)

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    plane: Plane | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Any = None,
    auth_check: Any = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # SUPPRESS'd subparser flags may leave attrs unset; normalize them.
    for name, default in (
        ("json", False),
        ("config", None),
        ("state_dir", None),
        ("verify", False),
        ("allow_open_permissions", False),
        ("providers", None),
        ("github", None),
        ("github_secret", None),
    ):
        if not hasattr(args, name):
            setattr(args, name, default)
    # Injectable seams for tests; argparse Namespace carries them along.
    args.plane = plane
    args.transport = transport
    args.auth_check = auth_check
    if sleep is not None:
        args.sleep = sleep
    elif not hasattr(args, "sleep"):
        import time

        args.sleep = time.sleep
    if getattr(args, "prompt", None) is None and getattr(args, "command", "") == "smoke":
        from sbx.smoke import DEFAULT_PROMPT

        args.prompt = DEFAULT_PROMPT
    env = _env_with(args)
    try:
        return int(args.func(args, env) or 0)
    except BootstrapError as exc:
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": {"code": exc.code, "message": exc.message, "hint": exc.hint},
                    }
                ),
                file=sys.stderr,
            )
        else:
            print(f"error[{exc.code}]: {exc.message}", file=sys.stderr)
            if exc.hint:
                print(f"hint: {exc.hint}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
