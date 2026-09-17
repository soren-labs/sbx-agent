"""``sbx doctor`` — verify a deployment end to end (SOR-98).

Reports presence, hash prefixes, and reachability only. It never prints a
token, password, or credential value — the bootstrap key appears solely as
its ``sha256:`` fingerprint, and local credential discovery (SOR-115)
reports presence/permission/schema/status only.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path

import httpx

from sbx.config import ResolvedConfig, key_path
from sbx.credentials import cli_auth_check, scan_credentials
from sbx.deploy import read_deploy_state
from sbx.httpapi import ApiError, V1Client
from sbx.keys import fingerprint, resolve_api_key
from sbx.plane import Plane
from sbx.prereqs import (
    Check,
    check_dict_present,
    check_modal_auth,
    check_modal_package,
    check_provider_config,
    check_python,
    check_secret_present,
)


def _api_checks(
    config_base_url: str,
    token: str | None,
    *,
    transport: httpx.BaseTransport | None,
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

    provider_lines = []
    for model in models.get("models", []):
        provider = model.get("provider", "?")
        free = model.get("accounts_available")
        provider_lines.append(f"{provider}:{free}")
    checks.append(
        Check(
            name="providers",
            ok=True,
            detail="model/account view: " + (", ".join(provider_lines) or "none"),
        )
    )
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
        return Check(
            name="account-secrets",
            ok=False,
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
            checks.append(check_secret_present(name in secret_names, name))
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

    # Same resolution order as `sbx status`: configured URL, else the last
    # deployed URL recorded in the state dir.
    base_url = config.api_base_url or str(read_deploy_state(env).get("app_url") or "")
    checks.extend(_api_checks(base_url, token, transport=transport))

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
