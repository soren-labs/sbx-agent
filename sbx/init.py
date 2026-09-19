"""``sbx init`` — toolchain check + config file generation (SOR-98).

Idempotent: an existing config keeps its file values; only explicit flags
overwrite. Tool problems are reported with remediation hints, not hidden —
init itself still writes config so the workflow stays resumable.

Init also scans the *selected* providers' local credential files
(``sbx.credentials`` — SOR-115): presence/permission/schema per declared
path, plus the provider CLI's own auth check under ``--verify``. Discovery
is advisory — it guides, it never blocks, and it never prints contents.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from sbx.config import (
    BootstrapConfig,
    ResolvedConfig,
    load,
    load_file_values,
    save,
    state_dir,
)
from sbx.credentials import CredentialScan, cli_auth_check, scan_credentials
from sbx.plane import Plane
from sbx.prereqs import (
    Check,
    check_github,
    check_modal_auth,
    check_provider_config,
    tool_checks,
)


@dataclass(frozen=True)
class InitReport:
    checks: tuple[Check, ...]
    config_path: Path
    config_created: bool
    state_dir: Path
    authenticated: bool
    credentials: tuple[CredentialScan, ...] = ()


def _merge_flags(
    base: BootstrapConfig,
    *,
    profile: str | None,
    app_name: str | None,
    base_url: str | None,
    providers: tuple[str, ...] | None,
    github_bridge: bool | None,
    github_secret_name: str | None,
) -> BootstrapConfig:
    overrides = {}
    if profile is not None:
        overrides["modal_profile"] = profile
    if app_name is not None:
        overrides["modal_app_name"] = app_name
    if base_url is not None:
        overrides["api_base_url"] = base_url
    if providers is not None:
        overrides["providers"] = providers
    # SOR-133: tri-state — None preserves the file value; an explicit flag
    # (incl. `--no-github-bridge`) overwrites it. An empty `--github-secret-name`
    # clears a persisted name.
    if github_bridge is not None:
        overrides["github_bridge"] = github_bridge
    if github_secret_name is not None:
        overrides["github_secret_name"] = github_secret_name or None
    return replace(base, **overrides) if overrides else base


def init(
    cfg: ResolvedConfig,
    plane: Plane,
    *,
    env: Mapping[str, str] | None = None,
    profile: str | None = None,
    app_name: str | None = None,
    base_url: str | None = None,
    providers: tuple[str, ...] | None = None,
    github_bridge: bool | None = None,
    github_secret_name: str | None = None,
    verify: bool = False,
    allow_open_permissions: bool = False,
    auth_check: Callable[[str, Path], str] | None = None,
) -> InitReport:
    env = os.environ if env is None else env
    checks = tool_checks()

    # Merge file values (not env-resolved, so overrides aren't frozen into
    # the file) with explicit flags, then persist.
    file_values = load_file_values(cfg.path)
    merged = _merge_flags(
        file_values,
        profile=profile,
        app_name=app_name,
        base_url=base_url,
        providers=providers,
        github_bridge=github_bridge,
        github_secret_name=github_secret_name,
    )
    save(merged, cfg.path, env=env)
    # Re-resolve post-write so the advisory github check reports what was
    # actually persisted, with env overrides still winning at run time.
    resolved = load(cfg.path, env=env)

    state_dir(env).mkdir(parents=True, exist_ok=True)

    workspace = None
    try:
        workspace = plane.workspace()
    except Exception:
        workspace = None
    auth = check_modal_auth(workspace, env=env)

    # Local credential discovery — only the providers the deployment will
    # actually build participate; unselected providers never block.
    selected = providers if providers is not None else cfg.config.providers
    if auth_check is None and verify:
        auth_check = partial(cli_auth_check, env=env)
    scans = scan_credentials(
        selected,
        env=env,
        allow_open_permissions=allow_open_permissions,
        auth_check=auth_check,
    )
    return InitReport(
        checks=tuple(
            [
                *checks,
                auth,
                check_provider_config(merged.providers),
                *(s.to_check() for s in scans),
                # SOR-117/SOR-133: advisory GitHub-bridge detection against
                # the just-persisted (and env-overridden) bridge config; the
                # ``gh auth status`` probe still only runs under --verify.
                check_github(
                    env,
                    verify=verify,
                    enabled=resolved.config.github_bridge,
                    secret_name=resolved.config.github_secret_name,
                ),
            ]
        ),
        config_path=cfg.path,
        config_created=not cfg.file_exists,
        state_dir=state_dir(env),
        authenticated=auth.ok,
        credentials=tuple(scans),
    )
