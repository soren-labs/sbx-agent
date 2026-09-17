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
from collections.abc import Callable, Mapping
from dataclasses import dataclass

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
