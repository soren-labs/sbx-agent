"""Prerequisite checks shared by ``sbx init`` and ``sbx doctor``.

Every check returns a :class:`Check` — name, ok/warn level, detail, and a
remediation ``hint`` so a missing prerequisite always produces an actionable
message instead of a bare failure.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import control.github as gh

from sbx.config import KNOWN_PROVIDERS
from sbx.errors import BootstrapError

MIN_PYTHON = (3, 12)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    hint: str | None = None
    warn: bool = False  # True → advisory; False → required

    @property
    def status(self) -> str:
        if self.ok:
            return "ok"
        return "warn" if self.warn else "fail"


def check_python() -> Check:
    current = sys.version_info[:2]
    ok = current >= MIN_PYTHON
    return Check(
        name="python",
        ok=ok,
        detail=f"python {current[0]}.{current[1]} (need >={MIN_PYTHON[0]}.{MIN_PYTHON[1]})",
        hint=None
        if ok
        else f"install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ and recreate the env",
    )


def check_modal_package() -> Check:
    ok = importlib.util.find_spec("modal") is not None
    return Check(
        name="modal-cli",
        ok=ok,
        detail="modal package installed" if ok else "modal package not importable",
        hint=None
        if ok
        else "install with `pip install modal` (or `uv sync`), then `modal token new`",
    )


def check_tool(name: str, install_hint: str, *, required: bool = False) -> Check:
    found = shutil.which(name) is not None
    return Check(
        name=name,
        ok=found,
        warn=not required,
        detail=f"{name} on PATH" if found else f"{name} not found on PATH",
        hint=None if found else install_hint,
    )


def check_modal_auth(workspace: str | None, env: Mapping[str, str] | None = None) -> Check:
    """Modal auth check: CLI profile login OR user-owned env tokens.

    ``MODAL_TOKEN_ID``/``MODAL_TOKEN_SECRET`` are a supported auth source —
    when both are set but the workspace probe still failed, the tokens
    themselves are the suspect, not a missing login.
    """
    env = os.environ if env is None else env
    env_id = bool((env.get("MODAL_TOKEN_ID") or "").strip())
    env_secret = bool((env.get("MODAL_TOKEN_SECRET") or "").strip())
    if workspace is not None:
        source = "env MODAL_TOKEN_ID/MODAL_TOKEN_SECRET" if env_id and env_secret else "profile"
        return Check(
            name="modal-auth",
            ok=True,
            detail=f"authenticated (workspace {workspace}, via {source})",
        )
    if env_id and env_secret:
        return Check(
            name="modal-auth",
            ok=False,
            detail="MODAL_TOKEN_ID/MODAL_TOKEN_SECRET are set but the workspace probe failed",
            hint="check the tokens are valid and belong to the intended workspace "
            "(`modal app list` should succeed), or unset them and run `modal token new`",
        )
    if env_id != env_secret:
        missing = "MODAL_TOKEN_SECRET" if env_id else "MODAL_TOKEN_ID"
        return Check(
            name="modal-auth",
            ok=False,
            detail=f"partial env credentials: {missing} is missing",
            hint="export both MODAL_TOKEN_ID and MODAL_TOKEN_SECRET, or run `modal token new`",
        )
    return Check(
        name="modal-auth",
        ok=False,
        detail="not authenticated",
        hint="run `modal token new` (or `modal setup`), or export "
        "MODAL_TOKEN_ID/MODAL_TOKEN_SECRET",
    )


def check_provider_config(providers: Sequence[str]) -> Check:
    """``deploy.providers`` must name at least one contract provider.

    Deploy preconditions derive from this list — an empty or unknown entry
    means the deployment cannot serve any provider.
    """
    enabled = [str(p) for p in providers]
    if not enabled:
        return Check(
            name="provider-config",
            ok=False,
            detail="no providers configured",
            hint="set deploy.providers in the config or SBX_PROVIDERS, "
            f'e.g. "{",".join(KNOWN_PROVIDERS[:2])}"',
        )
    unknown = [p for p in enabled if p not in KNOWN_PROVIDERS]
    if unknown:
        return Check(
            name="provider-config",
            ok=False,
            detail=f"unknown provider(s): {', '.join(unknown)}",
            hint=f"valid providers: {', '.join(KNOWN_PROVIDERS)}",
        )
    return Check(
        name="provider-config",
        ok=True,
        detail="enabled: " + ", ".join(enabled),
    )


def check_github(
    env: Mapping[str, str] | None = None,
    *,
    verify: bool = False,
    runner: Callable[..., Any] | None = None,
    which: Callable[[str], str | None] | None = None,
    gate: bool | None = None,
    secret_name: str | None = None,
) -> Check:
    """Optional GitHub auth bridge (SOR-117/SOR-133): advisory detection only.

    Reports which auth source exists (``GH_TOKEN``/``GITHUB_TOKEN`` env var
    name, ``gh auth status`` under ``verify``) and whether the bridge is
    armed — never token material. Always warn-or-ok: the GitHub-less path
    is fully supported.

    ``gate``/``secret_name`` carry the *resolved* bridge config when the
    caller has one (file + env); ``None`` falls back to the raw
    ``SBX_GITHUB_EPHEMERAL``/``SBX_GITHUB_SECRET_NAME`` env vars.
    """
    env = os.environ if env is None else env
    kwargs: dict[str, Any] = {"probe_gh": verify, "which": which}
    if runner is not None:
        kwargs["runner"] = runner
    det = gh.detect(env, **kwargs)
    opted = det.opted_in if gate is None else gate
    name = secret_name if secret_name is not None else env.get("SBX_GITHUB_SECRET_NAME")
    name = name.strip() if name else None
    if opted:
        if det.token_env:
            detail = (
                f"{det.token_env} detected; the GitHub bridge is armed — sandboxes "
                "get GH_TOKEN/GITHUB_TOKEN + a github.com credential helper"
            )
            if name:
                detail += f"; remote deploys mount Modal Secret {name!r}"
            return Check(name="github", ok=True, detail=detail)
        if name:
            return Check(
                name="github",
                ok=True,
                detail=f"GitHub bridge armed — Modal Secret {name!r} supplies the "
                "token to the deployed control plane (no host token needed)",
            )
        return Check(
            name="github",
            ok=False,
            warn=True,
            detail="GitHub bridge armed but no GH_TOKEN/GITHUB_TOKEN in the env",
            hint="export GH_TOKEN (or `export GH_TOKEN=$(gh auth token)` when the gh "
            "CLI is logged in); for a remote deploy, name a Modal Secret via "
            "github.secret_name / SBX_GITHUB_SECRET_NAME — or disable the bridge "
            "(`sbx init --no-github` / unset SBX_GITHUB_EPHEMERAL)",
        )
    if name:
        return Check(
            name="github",
            ok=False,
            warn=True,
            detail=f"GitHub bridge Secret {name!r} is configured but the bridge is off",
            hint="arm it with `sbx init --github` (github.ephemeral in config) or "
            "export SBX_GITHUB_EPHEMERAL=1 — a named Secret alone injects nothing",
        )
    if det.token_env:
        return Check(
            name="github",
            ok=False,
            warn=True,
            detail=f"{det.token_env} detected — sandbox GitHub injection is off",
            hint="export SBX_GITHUB_EPHEMERAL=1 to inject it into sandboxes "
            "(private-repo clone/push/PR on github.com)",
        )
    if det.gh_authenticated:
        return Check(
            name="github",
            ok=False,
            warn=True,
            detail="gh CLI is authenticated — no GH_TOKEN/GITHUB_TOKEN exported",
            hint="export GH_TOKEN=$(gh auth token) and SBX_GITHUB_EPHEMERAL=1 to "
            "enable sandbox GitHub auth",
        )
    tail = ""
    if det.gh_on_path:
        tail = "; gh CLI on PATH" + ("" if verify else " (auth not probed — pass --verify)")
    return Check(
        name="github",
        ok=True,
        detail=f"no GitHub auth detected — private-repo clone/push/PR disabled (optional){tail}",
    )


def check_secret_present(present: bool, name: str) -> Check:
    return Check(
        name=f"secret:{name}",
        ok=present,
        detail="present" if present else "missing",
        hint=None
        if present
        else (
            f"create it with `modal secret create {name} <KEY>=<value>` "
            "or rerun `sbx deploy` after providing the credential"
        ),
    )


def check_dict_present(present: bool, name: str) -> Check:
    return Check(
        name=f"dict:{name}",
        ok=present,
        warn=True,
        detail="present" if present else "not created yet",
        hint=None if present else "created automatically by `sbx deploy`",
    )


def require(checks: list[Check]) -> None:
    """Raise an actionable :class:`BootstrapError` on the first required failure."""
    for check in checks:
        if not check.ok and not check.warn:
            raise BootstrapError(
                f"{check.name}: {check.detail}",
                hint=check.hint,
                code="missing_prereq",
            )


def tool_checks() -> list[Check]:
    """Local toolchain checks used by ``sbx init``."""
    return [
        check_python(),
        check_modal_package(),
        check_tool("git", "install git to clone/update this repository"),
        check_tool("uv", "install uv (https://docs.astral.sh/uv) or use pip"),
    ]


def raise_for(checks: list[Check], render: Callable[[Check], None] | None = None) -> list[Check]:
    for check in checks:
        if render is not None:
            render(check)
    require(checks)
    return checks
