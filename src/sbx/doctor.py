"""``sbx doctor`` — verify a deployment end to end (SOR-98).

Reports presence, hash prefixes, and reachability only. It never prints a
token, password, or credential value — the bootstrap key appears solely as
its ``sha256:`` fingerprint, and local credential discovery (SOR-115)
reports presence/permission/schema/status only.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from control.config import ACTIVE_STATUSES
from control.provider_readiness import provider_readiness

from sbx.config import ResolvedConfig, key_path
from sbx.credentials import cli_auth_check, scan_credentials
from sbx.deploy import read_deploy_state
from sbx.httpapi import ApiError, V1Client
from sbx.keys import fingerprint, resolve_api_key
from sbx.plane import Plane
from sbx.prereqs import (
    Check,
    check_dict_present,
    check_github,
    check_modal_auth,
    check_modal_package,
    check_provider_config,
    check_python,
    check_secret_present,
)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def provider_summaries(models: Any) -> list[str]:
    """Aggregate ``/v1/models`` rows by provider — one line per provider.

    ``accounts_available`` is per (provider, model); identical counts render
    as ``devin: 1 account, 2 models (m1, m2)`` and divergent counts as a
    ``min–max`` range, so a provider is never repeated per model.
    """
    by_provider: dict[str, dict[str, int]] = {}
    for model in models or []:
        if not isinstance(model, Mapping):
            continue
        provider = str(model.get("provider") or "?")
        name = str(model.get("model") or model.get("id") or "?")
        try:
            free = int(model.get("accounts_available") or 0)
        except (TypeError, ValueError):
            free = 0
        by_provider.setdefault(provider, {})[name] = free
    lines: list[str] = []
    for provider in sorted(by_provider):
        entries = by_provider[provider]
        counts = sorted(set(entries.values()))
        if len(counts) == 1:
            accounts = _plural(counts[0], "account")
        else:
            accounts = f"{counts[0]}–{counts[-1]} accounts"
        names = ", ".join(sorted(entries))
        lines.append(f"{provider}: {accounts}, {_plural(len(entries), 'model')} ({names})")
    return lines


def live_agent_count(client: V1Client) -> int:
    """Live (non-terminal) agents across all ``/v1/agents`` pages.

    ``creating`` / ``idle`` / ``running`` agents each occupy a live-sandbox
    slot — an idle agent waiting for a follow-up still counts until it is
    closed (``DELETE /v1/agents/{id}``) or reaped.
    """
    live = 0
    cursor: str | None = None
    while True:
        page = client.list_agents(cursor=cursor)
        if not isinstance(page, Mapping):
            return live
        agents = page.get("agents") or []
        live += sum(
            1 for a in agents if isinstance(a, Mapping) and a.get("status") in ACTIVE_STATUSES
        )
        cursor = page.get("next_cursor")
        if not cursor:
            return live


def _live_agents_check(client: V1Client, max_concurrent: int | None) -> Check:
    try:
        live = live_agent_count(client)
    except (ApiError, httpx.HTTPError) as exc:
        return Check(
            name="live-agents",
            ok=False,
            warn=True,
            detail=f"cannot list agents: {exc}",
        )
    if max_concurrent is not None and live >= max_concurrent:
        return Check(
            name="live-agents",
            ok=False,
            warn=True,
            detail=f"{_plural(live, 'live agent')} — at the "
            f"SBX_MAX_CONCURRENT cap ({max_concurrent})",
            hint="close an idle agent (DELETE /v1/agents/{id}), run scoped "
            "cleanup (DELETE /v1/workflows/{id}), or raise "
            "deploy.max_concurrent / SBX_MAX_CONCURRENT and `sbx deploy`",
        )
    cap = (
        f"cap {max_concurrent} (SBX_MAX_CONCURRENT)"
        if max_concurrent is not None
        else "SBX_MAX_CONCURRENT unset — remote defaults apply"
    )
    return Check(
        name="live-agents",
        ok=True,
        detail=f"{_plural(live, 'live agent')}, {cap}; idle agents hold slots until closed",
    )


def _api_checks(
    config_base_url: str,
    token: str | None,
    *,
    transport: httpx.BaseTransport | None,
    max_concurrent: int | None = None,
) -> list[Check]:
    checks: list[Check] = []
    if not config_base_url:
        checks.append(
            Check(
                name="api-url",
                ok=False,
                detail="api.base_url is not configured",
                hint="run `sbx deploy`, or set api.base_url / SBX_BASE_URL",
            )
        )
        return checks
    checks.append(Check(name="api-url", ok=True, detail=config_base_url))

    # SOR-211/SOR-217: the same-origin Console is part of the Platform
    # surface — an unauthenticated GET / must serve the app shell.
    try:
        with V1Client(config_base_url, transport=transport, timeout=10.0) as client:
            resp = client.fetch("/")
        if resp.status_code == 200 and (
            "html" in (resp.headers.get("content-type") or "").lower()
            or "<html" in resp.text[:500].lower()
        ):
            checks.append(Check(name="console", ok=True, detail="Console served at /"))
        else:
            checks.append(
                Check(
                    name="console",
                    ok=False,
                    detail=f"/ answered HTTP {resp.status_code} — no Console page",
                    hint="the deployment predates the same-origin Console — "
                    "rerun `sbx deploy` to upgrade (SOR-211)",
                )
            )
    except httpx.HTTPError:
        # api-reachable below reports the connectivity failure.
        pass

    try:
        with V1Client(config_base_url, transport=transport, timeout=10.0) as client:
            client.me()
        checks.append(
            Check(
                name="api-reachable",
                ok=False,
                warn=True,
                detail="/v1/me answered 200 without a token",
                hint="the app should challenge with 401 — check auth wiring",
            )
        )
    except ApiError as exc:
        if exc.status == 401:
            checks.append(Check(name="api-reachable", ok=True, detail="/v1 challenges auth"))
        else:
            checks.append(
                Check(
                    name="api-reachable",
                    ok=False,
                    warn=True,
                    detail=f"/v1 answered HTTP {exc.status} ({exc.code})",
                )
            )
    except httpx.HTTPError as exc:
        checks.append(
            Check(
                name="api-reachable",
                ok=False,
                detail=f"cannot reach {config_base_url}: {exc}",
                hint="check api.base_url and that `sbx deploy` finished; "
                "cold start can take a minute",
            )
        )
        return checks

    if token is None:
        checks.append(
            Check(
                name="api-auth",
                ok=False,
                warn=True,
                detail="no bootstrap key available locally",
                hint="run `sbx deploy` to mint one, or export SBX_API_KEY",
            )
        )
        return checks
    try:
        with V1Client(config_base_url, token, transport=transport, timeout=10.0) as client:
            me = client.me()
            checks.append(
                Check(
                    name="api-auth",
                    ok=True,
                    detail=f"key {me.get('key_id', me.get('id', '?'))} "
                    f"scopes={me.get('scopes', [])}",
                )
            )
            models = client.models()
            # Never raises ApiError/HTTPError — failures render as a warn
            # check, so a broken agent list can't mask api-auth's verdict.
            live_check = _live_agents_check(client, max_concurrent)
    except (ApiError, httpx.HTTPError) as exc:
        detail = getattr(exc, "code", None) or str(exc)
        checks.append(
            Check(
                name="api-auth",
                ok=False,
                detail=f"/v1 rejected the local key ({detail})",
                hint="the deployed bootstrap Secret may hold a different key — "
                "delete the state-dir key file and rerun `sbx deploy` to rotate",
            )
        )
        return checks

    provider_lines = provider_summaries(models.get("models", []))
    checks.append(
        Check(
            name="providers",
            ok=True,
            detail="provider/account view: " + (", ".join(provider_lines) or "none"),
        )
    )

    # SOR-217/SOR-221: the provider catalog is a Platform surface — every
    # contract provider must appear with its runtime + connection split.
    # Connection state is provider health and NEVER gates the verdict:
    # zero connected providers still means a healthy platform.
    try:
        with V1Client(config_base_url, token, transport=transport, timeout=10.0) as client:
            rows = client.providers().get("providers") or []
    except ApiError as exc:
        checks.append(
            Check(
                name="provider-catalog",
                ok=False,
                detail=f"/v1/providers answered {exc.status} ({exc.code})",
                hint="the deployment predates the provider catalog — "
                "rerun `sbx deploy` to upgrade (SOR-221)",
            )
        )
    else:
        names = [str(r.get("provider")) for r in rows if isinstance(r, Mapping)]
        checks.append(
            Check(
                name="provider-catalog",
                ok=True,
                detail=f"catalog lists {_plural(len(names), 'provider')}"
                + (f" ({', '.join(sorted(names))})" if names else ""),
            )
        )
        # SOR-258: fetch account detail once for the busy-vs-needs-login
        # split; an admin-scoped key is the common case for `sbx doctor`,
        # and a refusal simply keeps the busy convention.
        statuses: dict[str, list[str]] | None = None
        try:
            with V1Client(config_base_url, token, transport=transport, timeout=10.0) as client:
                account_rows = client.list_accounts().get("accounts") or []
            statuses = {}
            for a in account_rows:
                if isinstance(a, Mapping):
                    statuses.setdefault(str(a.get("provider")), []).append(
                        str(a.get("status") or "")
                    )
        except ApiError:
            statuses = None
        conn_bits = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            conn = row.get("connection") or {}
            runtime = row.get("runtime") or {}
            word = provider_readiness(
                row,
                account_statuses=(
                    statuses.get(str(row.get("provider"))) if statuses is not None else None
                ),
            )
            conn_bits.append(
                f"{row.get('provider', '?')}: {word} "
                f"({conn.get('status', '?')}/runtime:{runtime.get('status', '?')})"
            )
        checks.append(
            Check(
                name="provider-connection",
                ok=True,
                detail=", ".join(conn_bits) or "no provider accounts connected",
            )
        )
    checks.append(live_check)
    return checks


def _account_secret_check(config, plane: Plane, secret_names: set[str]) -> Check:
    """Verify every enabled-provider account record's referenced Secret exists.

    This catches the fresh-install failure mode where onboarding metadata and
    credential blobs exist in the accounts Dict but the runtime Secret was
    never materialized.  Values are never read or rendered.  Accounts of
    providers that are not enabled carry no prerequisite (SOR-116).
    """
    enabled = frozenset(config.providers)
    try:
        items = plane.dict_items(config.accounts_dict)
    except Exception as exc:
        return Check(
            name="account-secrets",
            ok=False,
            detail=f"cannot inspect account registry: {exc}",
            hint="check Modal Dict access, then rerun `sbx doctor`",
        )
    referenced: list[str] = []
    for key, value in items:
        if not (isinstance(key, str) and key.startswith("account/") and isinstance(value, dict)):
            continue
        if str(value.get("provider") or "") not in enabled:
            continue
        name = str(value.get("secret_name") or "").strip()
        if name:
            referenced.append(name)
    missing = sorted(name for name in set(referenced) if name not in secret_names)
    if missing:
        preview = ", ".join(missing[:5])
        if len(missing) > 5:
            preview += f", +{len(missing) - 5} more"
        # SOR-217: provider credential gaps are provider health — warn,
        # never a Platform failure.
        return Check(
            name="account-secrets",
            ok=False,
            warn=True,
            detail=f"missing {len(missing)} referenced account Secret(s): {preview}",
            hint="rerun `sbx deploy` to materialize imported account credentials",
        )
    return Check(
        name="account-secrets",
        ok=True,
        detail=f"{len(set(referenced))} referenced account Secret(s) present",
    )


def run_doctor(
    cfg: ResolvedConfig,
    plane: Plane,
    *,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    verify: bool = False,
    allow_open_permissions: bool = False,
    auth_check: Callable[[str, Path], str] | None = None,
) -> list[Check]:
    """Run every check; the caller decides how to render and exit."""
    env = os.environ if env is None else env
    config = cfg.config
    checks: list[Check] = [check_python(), check_modal_package()]

    workspace = None
    try:
        workspace = plane.workspace()
    except Exception:
        workspace = None
    checks.append(check_modal_auth(workspace, env=env))

    provider_check = check_provider_config(config.providers)
    checks.append(provider_check)

    secret_names: set[str] = set()
    secret_list_failed = False
    if workspace is not None:
        try:
            secret_names = plane.list_secret_names()
        except Exception:
            secret_list_failed = True
            checks.append(
                Check(
                    name="secrets",
                    ok=False,
                    detail="cannot list Modal secrets",
                    hint="check Modal auth (`modal token new`) then rerun `sbx doctor`",
                )
            )
    if workspace is None:
        # Unauthenticated: secret/dict presence is unknown, not missing.
        checks.append(
            Check(
                name="secrets",
                ok=False,
                warn=True,
                detail="skipped (not authenticated)",
            )
        )
    elif not secret_list_failed:
        for name in config.secret_names():
            # The shared Codex Secret only gates a codex deploy — an
            # unselected provider must not block onboarding (SOR-115).
            if name == config.codex_secret and "codex" not in config.providers:
                continue
            check = check_secret_present(name in secret_names, name)
            if name == config.codex_secret:
                # SOR-217: a provider credential is provider health — warn
                # so the Platform verdict stays independent.
                check = replace(check, warn=True)
            checks.append(check)
        # SOR-133: the armed GitHub bridge's named Secret is a deploy
        # prerequisite (``sbx deploy`` fails on it) — report its presence
        # here too. Operator-managed, so it is not in ``secret_names()``.
        if config.github_ephemeral and config.github_secret_name:
            checks.append(
                check_secret_present(
                    config.github_secret_name in secret_names,
                    config.github_secret_name,
                )
            )
        for name in config.dict_names():
            try:
                present = plane.has_dict(name)
            except Exception:
                present = False
            checks.append(check_dict_present(present, name))
        if provider_check.ok and plane.has_dict(config.accounts_dict):
            checks.append(_account_secret_check(config, plane, secret_names))

    token = resolve_api_key(env)
    if token is None:
        checks.append(
            Check(
                name="bootstrap-key",
                ok=False,
                warn=True,
                detail="no local key file",
                hint="run `sbx deploy` to generate one, or export SBX_API_KEY",
            )
        )
    else:
        path = key_path(env)
        mode = oct(path.stat().st_mode & 0o777) if path.exists() else "env"
        checks.append(
            Check(
                name="bootstrap-key",
                ok=True,
                detail=f"{fingerprint(token)} (mode {mode})",
            )
        )

    # Local credential discovery for the selected providers — advisory
    # only: the deployment may be serving credentials imported earlier, so
    # a missing local file is guidance, not a failed deployment.
    if auth_check is None and verify:
        auth_check = partial(cli_auth_check, env=env)
    for scan in scan_credentials(
        config.providers,
        env=env,
        allow_open_permissions=allow_open_permissions,
        auth_check=auth_check,
    ):
        checks.append(scan.to_check())

    # Optional GitHub bridge (SOR-117/SOR-133): advisory detection against
    # the resolved config — never prints a token; the ``gh auth status``
    # probe only runs under --verify.
    checks.append(
        check_github(
            env,
            verify=verify,
            gate=config.github_ephemeral,
            secret_name=config.github_secret_name,
        )
    )

    # Same resolution order as `sbx status`: configured URL, else the last
    # deployed URL recorded in the state dir.
    base_url = config.api_base_url or str(read_deploy_state(env).get("app_url") or "")
    checks.extend(
        _api_checks(
            base_url,
            token,
            transport=transport,
            max_concurrent=config.max_concurrent,
        )
    )

    app_url = None
    if workspace is not None:
        try:
            app_url = plane.app_url(config.modal_app_name)
        except Exception:
            app_url = None
    checks.append(
        Check(
            name="app",
            ok=app_url is not None,
            warn=True,
            detail=app_url or f"app {config.modal_app_name!r} not deployed",
            hint=None if app_url else "run `sbx deploy`",
        )
    )

    if workspace is not None:
        try:
            sandboxes = plane.list_sandboxes(config.modal_app_name)
            checks.append(
                Check(
                    name="cleanup",
                    ok=True,
                    detail=f"sandbox listing works ({len(sandboxes)} live)",
                )
            )
        except Exception as exc:
            checks.append(
                Check(
                    name="cleanup",
                    ok=False,
                    warn=True,
                    detail=f"cannot list sandboxes: {exc}",
                    hint="uninstall/doctor need Sandbox.list permission on the workspace",
                )
            )
    return checks


def failed(checks: list[Check]) -> list[Check]:
    """Required (non-warn) checks that did not pass."""
    return [c for c in checks if not c.ok and not c.warn]
