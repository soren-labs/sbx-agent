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
import webbrowser
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
    """Platform + Runtime + Account summary (SOR-217).

    ``platform`` is independent of provider health: it is ``healthy`` when
    the control plane answers ``/v1/me`` with the local key — zero
    connected providers still reports a healthy platform. ``runtime``
    (per-provider deploy evidence) and ``accounts`` (connection state)
    come from ``/v1/providers``; a pre-catalog deployment leaves them
    ``None`` rather than failing.
    """
    from sbx.deploy import read_deploy_state
    from sbx.doctor import live_agent_count, provider_summaries
    from sbx.httpapi import ApiError, V1Client
    from sbx.keys import fingerprint, resolve_api_key

    cfg = _resolve(args, env)
    state = read_deploy_state(env)
    token = resolve_api_key(env)
    base_url = cfg.config.api_base_url or state.get("app_url") or ""
    providers: Any = "unknown"
    live_agents: int | None = None
    runtime: dict[str, Any] | None = None
    accounts: dict[str, Any] | None = None
    if not base_url:
        platform_status = "not_deployed"
    elif token is None:
        platform_status = "unverified"  # reachable or not — no key to prove auth
    else:
        platform_status = "unreachable"
        try:
            with V1Client(base_url, token, transport=args.transport, timeout=10.0) as client:
                try:
                    client.me()
                    platform_status = "healthy"
                except ApiError as exc:
                    platform_status = (
                        "auth_failed" if exc.status in (401, 403) else f"error:{exc.status}"
                    )
                    raise
                models = client.models().get("models", [])
                providers = provider_summaries(models)
                try:
                    rows = client.providers().get("providers") or []
                    runtime = {
                        str(r.get("provider")): r.get("runtime")
                        for r in rows
                        if isinstance(r, Mapping)
                    }
                    accounts = {
                        "connected": sorted(
                            str(r.get("provider"))
                            for r in rows
                            if isinstance(r, Mapping)
                            and isinstance(r.get("connection"), Mapping)
                            and r["connection"].get("status") == "connected"
                        ),
                        "providers": {
                            str(r.get("provider")): r.get("connection")
                            for r in rows
                            if isinstance(r, Mapping)
                        },
                    }
                except ApiError:
                    pass  # pre-catalog deployment — runtime/accounts stay None
                try:
                    live_agents = live_agent_count(client)
                except Exception:
                    live_agents = None
        except ApiError:
            providers = "auth-failed" if platform_status == "auth_failed" else "error"
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
        # SOR-217 sections — platform health is independent of provider
        # health; runtime/accounts are None on a pre-catalog deployment.
        "platform": {
            "status": platform_status,
            "base_url": base_url or None,
            "deployed_version": state.get("version"),
            "deployed_at": state.get("deployed_at"),
        },
        "runtime": runtime,
        "accounts": accounts,
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
    print(f"platform:  {platform_status}" + (f" — {base_url}" if base_url else ""))
    print(f"config:    {payload['config_path']} ({'file' if cfg.file_exists else 'defaults'})")
    print(f"app:       {payload['app']}")
    print(f"base:      {base_url or '-'}")
    print(f"deploy:    {state.get('version') or 'never'} at {state.get('deployed_at') or '-'}")
    print(f"key:       {payload['key_fingerprint'] or 'none (run `sbx deploy`)'}")
    if runtime is not None:
        rt_line = "; ".join(
            f"{name} {info.get('status', '?')}"
            + (f" ({info['version']})" if info.get("version") else "")
            + (
                f" — {info['detail']}"
                if info.get("status") == "degraded" and info.get("detail")
                else ""
            )
            for name, info in sorted(runtime.items())
            if isinstance(info, Mapping)
        )
        print(f"runtime:   {rt_line or '-'}")
    if accounts is not None:
        acct_line = "; ".join(
            f"{name} {conn.get('status', '?')} "
            f"({conn.get('accounts_available', 0)}/{conn.get('accounts_total', 0)} accounts)"
            for name, conn in sorted(accounts["providers"].items())
            if isinstance(conn, Mapping)
        )
        print(f"accounts:  {acct_line or 'none connected'}")
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


# --------------------------------------------------------------------- auth


def _verify_remote_strict(client: Any, account_id: str) -> dict[str, Any]:
    """One ``POST /v1/accounts/{id}/verify`` — SOR-217's strict contract.

    Probes the stored credential in a throwaway sandbox server-side and
    returns ``{ok, account_id, provider, status, last_error}``. Transport
    and verification failures raise ``account_verify_failed``.
    """
    from sbx.httpapi import ApiError

    try:
        account = client.verify_account(account_id)
    except ApiError as exc:
        if exc.status == 404:
            hint = "no such account — list them with `sbx status` (accounts section)"
        elif exc.status in (401, 403):
            hint = "account verification needs the admin scope — use the bootstrap key"
        else:
            hint = "check `sbx doctor` — the deployment must serve /v1/accounts/{id}/verify"
        raise BootstrapError(
            f"cannot verify account {account_id}: {exc.message}",
            hint=hint,
            code="account_verify_failed",
        ) from exc
    except httpx.HTTPError as exc:
        raise BootstrapError(
            f"cannot reach the control plane: {exc}",
            hint="check api.base_url and that `sbx deploy` finished; `sbx doctor` diagnoses",
            code="account_verify_failed",
        ) from exc
    status = str(account.get("status") or "")
    last_error = account.get("last_error")
    if status != "active":
        raise BootstrapError(
            f"account {account_id} verification failed — status {status or 'unknown'}"
            + (f" ({last_error})" if last_error else ""),
            hint="re-import the credential (`sbx auth import-existing --provider "
            "<provider>`), then rerun `sbx auth verify`",
            code="account_verify_failed",
        )
    return {
        "ok": True,
        "account_id": account_id,
        "provider": account.get("provider"),
        "status": status,
        "last_error": last_error,
    }


# -------------------------------------------------------------------- deploy


def _print_deploy(report: Any, env: Mapping[str, str]) -> None:
    from sbx.config import key_path

    for step in report.steps:
        mark = "*" if step.changed else "="
        print(f"{mark} {step.name}: {step.detail}")
    print(f"deployed {report.version} → {report.base_url}")
    if report.degraded_providers:
        print("providers degraded (platform healthy — provider health is separate):")
        for name, why in report.degraded_providers.items():
            print(f"  {name}: {why}")
    if report.key_created:
        print(f"bootstrap key minted — saved to {key_path(env)} (mode 0600)")
    if report.key_rotated:
        print("bootstrap key rotated — remote secret now matches the new local key")
    print("use with the client:")
    print(f"  export SBX_BASE_URL={report.base_url}")
    print(f"  export SBX_API_KEY=$(cat {key_path(env)})")


def _modal_login(args: argparse.Namespace, env: Mapping[str, str], plane: Plane) -> Any:
    """The interactive `modal token new` lane for deploy/upgrade (SOR-209).

    Only armed on a real TTY with no env-token credentials — CI and
    non-interactive shells keep the fail-fast ``modal_auth_missing`` path.
    ``args.modal_login`` is the test seam.
    """
    login = getattr(args, "modal_login", None)
    if login is not None:
        return login
    if env.get("MODAL_TOKEN_ID") and env.get("MODAL_TOKEN_SECRET"):
        return None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    candidate = getattr(plane, "interactive_login", None)
    return candidate if callable(candidate) else None


def cmd_deploy(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from sbx.deploy import deploy

    cfg = _resolve(args, env)
    plane = _plane(cfg, args)
    report = deploy(
        cfg,
        plane,
        env=env,
        transport=args.transport,
        sleep=args.sleep,
        probe_attempts=args.probe_attempts,
        versions_lock=args.versions_lock,
        modal_login=_modal_login(args, env, plane),
    )
    if args.json:
        _emit_json(
            {
                "ok": True,
                "base_url": report.base_url,
                "version": report.version,
                "cli_versions": report.cli_versions or {},
                # SOR-217: provider-health gaps are reported, not fatal.
                "degraded_providers": report.degraded_providers or {},
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
                # SOR-217: the Platform verdict is independent of provider
                # health — warn-level checks (missing provider credentials,
                # unconnected providers) never flip it.
                "platform": {"status": "healthy" if not bad else "unhealthy"},
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
                "provider": result.provider,
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
    plane = _plane(cfg, args)
    report = upgrade(
        cfg,
        plane,
        env=env,
        transport=args.transport,
        sleep=args.sleep,
        probe_attempts=args.probe_attempts,
        version=args.version,
        versions_lock=args.versions_lock,
        modal_login=_modal_login(args, env, plane),
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


# --------------------------------------------------------------------- open


def cmd_open(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """Open the Console in a browser via a one-time grant handoff (SOR-211).

    The long-lived ``sbx_`` bootstrap key never enters a URL or log: we
    redeem it server-side for a single-use, short-TTL ticket
    (``POST /v1/console/grant``), hand the browser the ticket in the URL
    fragment — never sent to the server — and the Console exchanges it for
    a fresh minted key (``POST /v1/console/exchange``).
    """
    from sbx.deploy import read_deploy_state
    from sbx.httpapi import ApiError, V1Client
    from sbx.keys import resolve_api_key

    cfg = _resolve(args, env)
    base_url = args.base_url or cfg.config.api_base_url
    if not base_url:
        base_url = str(read_deploy_state(env).get("app_url") or "")
    if not base_url:
        raise BootstrapError(
            "no control-plane URL — nothing deployed (or configured) yet",
            hint="run `sbx deploy` first, or pass --base-url / set SBX_BASE_URL",
            code="no_deployment",
        )
    token = resolve_api_key(env)
    if token is None:
        raise BootstrapError(
            "no sbx_ API key found",
            hint="run `sbx deploy` (mints the bootstrap key) or export SBX_API_KEY",
            code="api_key_missing",
        )
    try:
        with V1Client(base_url, token, transport=args.transport, timeout=15.0) as client:
            grant = client.post("/v1/console/grant")
    except ApiError as exc:
        raise BootstrapError(
            f"cannot mint a console grant: {exc.message}",
            hint="check `sbx doctor` — the deployment must serve /v1/console/grant",
            code="grant_failed",
        ) from exc
    ticket = str(grant.get("grant") or "")
    if not ticket:
        raise BootstrapError(
            "the control plane returned an empty console grant",
            code="grant_failed",
        )
    url = f"{base_url.rstrip('/')}/#/connect?grant={ticket}"
    if args.json:
        _emit_json(
            {
                "ok": True,
                "base_url": base_url,
                "url": url,
                "expires_in": grant.get("expires_in"),
            }
        )
        return 0
    if args.print:
        print(url)
        return 0
    ttl = grant.get("expires_in")
    if webbrowser.open(url):
        print(f"opened the Console in your browser (one-time grant expires in {ttl}s)")
    else:
        print("no browser found — open this URL yourself:")
        print(f"  {url}")
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


# -------------------------------------------------------------------- auth


def _models_arg(value: str | None) -> tuple[str, ...] | None:
    if not value:
        return None
    return tuple(m.strip() for m in value.split(",") if m.strip())


def cmd_auth(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """``sbx auth`` dispatch — canonical auth surface (SOR-213/SOR-216)."""
    from sbx import auth as auth_mod

    cfg = _resolve(args, env)
    action = args.auth_cmd
    # The remote verify lane only applies to writes/probes when the shared
    # cloud store is in use; ``--local`` always forces the sandbox probe.
    client = (
        None
        if getattr(args, "local", False) or action == "status"
        else auth_mod.remote_client(cfg, env, transport=args.transport)
    )
    service = auth_mod.make_auth_service(
        env,
        probe=args.probe,
        secret_writer=args.secret_writer,
        login_runner=args.login_runner,
    )
    try:
        if action == "status":
            payload = auth_mod.auth_status(service, env, provider=args.provider)
        elif action == "login":
            payload = auth_mod.auth_login(
                service,
                env,
                provider=args.provider,
                account_id=args.account_id,
                label=args.label,
                slots=args.slots,
                models=_models_arg(args.models),
                no_verify=args.no_verify,
                experimental_ok=args.experimental,
                allow_open_permissions=args.allow_open_permissions,
                client=client,
            )
        elif action == "import-existing":
            payload = auth_mod.auth_import_existing(
                service,
                env,
                provider=args.provider,
                source=args.source,
                account_id=args.account_id,
                label=args.label,
                slots=args.slots,
                models=_models_arg(args.models),
                no_verify=args.no_verify,
                experimental_ok=args.experimental,
                allow_open_permissions=args.allow_open_permissions,
                client=client,
            )
        elif action == "verify":
            # Remote lane (SOR-217): a deployment is configured → probe the
            # plane's own accounts server-side with the strict contract.
            # No deployment (or --local) → the local sandbox probe.
            dep = (
                None
                if getattr(args, "local", False)
                else auth_mod.deployment_client(cfg, env, transport=args.transport)
            )
            if dep is not None:
                results = [
                    _verify_remote_strict(dep, aid) for aid in auth_mod.targets_for(service, args)
                ]
                payload = (
                    results[0]
                    if len(results) == 1
                    else {"accounts": results, "verified": all(r["ok"] for r in results)}
                )
            else:
                payload = auth_mod.auth_verify(service, env, args=args, client=None)
        elif action == "relink":
            payload = auth_mod.auth_relink(service, env, args=args, client=client)
        elif action == "logout":
            # The managed-Secret deleter only exists on the modal lane;
            # a file-store logout never touches it. An injected plane
            # (tests) wins either way.
            plane = args.plane or (_plane(cfg, args) if env.get("SBX_BACKEND") == "modal" else None)
            payload = auth_mod.auth_logout(service, env, args=args, plane=plane)
        else:
            raise BootstrapError(f"unknown auth action {action!r}", code="unknown_action")
    except BootstrapError:
        raise
    if args.json:
        _emit_json(payload)
        return 0
    _print_auth_payload(action, payload)
    return 0


def _print_auth_payload(action: str, payload: dict[str, Any]) -> None:
    if action == "status":
        for scan in payload["local"]:
            print(
                f"local\t{scan['provider']}\t{scan['status']}\t"
                f"{scan['path'] or '-'}\t{scan['detail']}"
            )
        for s in payload["accounts"]:
            print(
                f"acct\t{s['account_id']}\t{s['provider']}\t"
                f"status={s['status']}\tauth={s['auth_state']}\t"
                f"running={s['running']}/{s['max_concurrent']}"
            )
        return
    accounts = payload.get("accounts") or [payload]
    for entry in accounts:
        if "ok" in entry and "status" in entry and "session" not in entry:
            # Remote verify lane (SOR-217 shape): {ok, account_id, provider,
            # status, last_error} rather than an AuthSession dict.
            print(
                f"account {entry.get('account_id', '?')}: verified — "
                f"status {entry.get('status') or '?'} "
                f"(provider {entry.get('provider') or '?'})"
            )
            continue
        session = entry.get("session") or {}
        line = f"{entry.get('account_id', '?')}\tauth={session.get('auth_state', '?')}"
        if "verified" in entry:
            line += f"\tverified={bool(entry.get('verified'))}"
        if entry.get("lane"):
            line += f"\tlane={entry['lane']}"
        if entry.get("probe"):
            line += f"\tprobe={entry['probe']}"
        if entry.get("removed"):
            line += f"\tremoved={len(entry['removed'])}"
        print(line)


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
        "open",
        parents=[sub_common],
        help="open the Console in a browser via a one-time grant handoff",
    )
    p.add_argument(
        "--base-url",
        help="control-plane URL (default: config api_base_url, else the deployed app URL)",
    )
    p.add_argument(
        "--print",
        dest="print",
        action="store_true",
        help="print the one-time Console URL instead of opening a browser",
    )
    p.set_defaults(func=cmd_open)

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

    p = sub.add_parser(
        "auth",
        parents=[sub_common],
        help="provider auth: login/status/verify/relink/logout/import-existing",
    )
    auth_sub = p.add_subparsers(dest="auth_cmd", required=True)

    a = auth_sub.add_parser(
        "status",
        parents=[sub_common],
        help="local credential scan + per-account auth-session states",
    )
    a.add_argument("--provider", help="limit to one provider")
    a.set_defaults(func=cmd_auth)

    a = auth_sub.add_parser(
        "login",
        parents=[sub_common],
        help="run the provider's official CLI/OAuth login, then capture + verify",
    )
    _auth_write_args(a)
    a.set_defaults(func=cmd_auth)

    a = auth_sub.add_parser(
        "import-existing",
        parents=[sub_common],
        help="capture credential files a vendor login already wrote (no token paste)",
    )
    _auth_write_args(a)
    a.add_argument(
        "--from",
        dest="source",
        default=None,
        help="credential file/dir/blob to import (default: declared files under $HOME)",
    )
    a.set_defaults(func=cmd_auth)

    a = auth_sub.add_parser(
        "verify",
        parents=[sub_common],
        help="run the cloud auth probe; promotes the account on a pass "
        "(remote /v1 probe when a deployment is configured, else local)",
    )
    a.add_argument(
        "account_id_pos",
        nargs="?",
        metavar="account_id",
        help="account id to verify (e.g. acct-devin-...)",
    )
    _auth_target_args(a)
    a.add_argument(
        "--local",
        action="store_true",
        help="force the local sandbox probe instead of the remote /v1 verify",
    )
    a.set_defaults(func=cmd_auth)

    a = auth_sub.add_parser(
        "relink",
        parents=[sub_common],
        help="re-capture → refresh → verify; restores scheduler eligibility",
    )
    _auth_target_args(a)
    a.add_argument(
        "--from",
        dest="source",
        default=None,
        help="credential file/dir/blob to re-capture (default: $HOME)",
    )
    a.add_argument(
        "--relogin",
        action="store_true",
        help="run the official login flow first (grant is dead locally)",
    )
    a.add_argument("--no-verify", action="store_true", help="skip the post-relink verify")
    a.add_argument(
        "--allow-open-permissions",
        action="store_true",
        help="accept credential files readable by group/other",
    )
    a.set_defaults(func=cmd_auth)

    a = auth_sub.add_parser(
        "logout",
        parents=[sub_common],
        help="sign out: drop credential material, flip to unverified",
    )
    _auth_target_args(a)
    a.add_argument(
        "--local",
        action="store_true",
        help="also delete the provider's credential files under $HOME",
    )
    a.add_argument(
        "--keep-secret",
        action="store_true",
        help="preserve the managed sbx-acct-<id> Secret",
    )
    a.set_defaults(func=cmd_auth)

    return parser


def _auth_target_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--account-id", default=None, help="act on one account")
    p.add_argument("--provider", default=None, help="act on all accounts of a provider")


def _auth_write_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--provider", required=True, help="provider to authenticate")
    p.add_argument("--account-id", default=None, help="account id to create/relink")
    p.add_argument("--label", default="", help="account label")
    p.add_argument("--slots", type=int, default=1, help="max_concurrent")
    p.add_argument("--models", default=None, help="comma-separated advertised models")
    p.add_argument("--no-verify", action="store_true", help="skip the post-import verify")
    p.add_argument("--experimental", action="store_true", help="allow experimental providers")
    p.add_argument(
        "--allow-open-permissions",
        action="store_true",
        help="accept credential files readable by group/other",
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    plane: Plane | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Any = None,
    auth_check: Any = None,
    modal_login: Any = None,
    login_runner: Any = None,
    secret_writer: Any = None,
    probe: Any = None,
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
        ("base_url", None),
        ("print", False),
        ("provider", None),
        ("account_id", None),
        ("account_id_pos", None),
        ("label", ""),
        ("slots", 1),
        ("models", None),
        ("source", None),
        ("no_verify", False),
        ("experimental", False),
        ("relogin", False),
        ("local", False),
        ("keep_secret", False),
    ):
        if not hasattr(args, name):
            setattr(args, name, default)
    # Injectable seams for tests; argparse Namespace carries them along.
    args.plane = plane
    args.transport = transport
    args.auth_check = auth_check
    args.modal_login = modal_login
    args.login_runner = login_runner
    args.secret_writer = secret_writer
    args.probe = probe
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
