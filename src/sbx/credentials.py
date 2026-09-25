"""Local provider-credential discovery for first-run onboarding (SOR-115).

Scans each *selected* provider's declared credential locations under
``$HOME`` and reports presence / permission / schema / auth status plus the
official remediation — never the credential contents. Statuses:

- ``not_found``          no file at the declared path
- ``permission_invalid`` present but group/other accessible (mode & 0077)
- ``schema_invalid``     present but not the provider's credential shape
- ``discovered``         present, private, schema-clean — usable
- ``verified``           the provider CLI's own auth check passed
- ``auth_invalid``       the provider CLI rejected the credential
- ``skipped``            provider has no declared credential files

``verified`` / ``auth_invalid`` are only produced when an ``auth_check``
runs (``sbx init --verify`` / ``sbx credentials --verify`` /
``sbx doctor --verify``): a plain scan caps at ``discovered`` — a file on
disk is not proof the provider still accepts it. The authoritative check is
the provider CLI's own auth command (``control.onboarding
PROVIDER_AUTH_CHECKS``); ``verify --probe auth`` runs the same check inside
a throwaway sandbox against an imported credential.
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from control.onboarding import (
    OnboardingError,
    _check_content_schema,
    classify_auth_output,
    descriptor_for,
    provider_auth_argv,
)

from sbx.prereqs import Check

DISCOVERY_STATUSES: tuple[str, ...] = (
    "not_found",
    "permission_invalid",
    "schema_invalid",
    "discovered",
    "verified",
    "auth_invalid",
)

# Official login entry points — keep in sync with the README provider table
# and docs/providers.md.
LOGIN_HINTS: dict[str, str] = {
    "codex": "run `codex login`",
    "devin": "run `devin` and complete the interactive login",
    "antigravity": "run `agy` and complete the OAuth login",
    "grok": "run `grok` and log in",
    "opencode": "run `opencode auth login`",
}

# Worst-problem ranking when a provider declares several files: a missing
# declared file is worse than a present-and-clean one.
_SEVERITY = {
    "discovered": 0,
    "verified": 0,
    "not_found": 1,
    "permission_invalid": 2,
    "schema_invalid": 3,
    "auth_invalid": 4,
}

# Child env for a host-side auth check — deliberately scrubbed so ambient
# credential variables (OPENAI_API_KEY, XAI_API_KEY, ACP_BACKEND, …) cannot
# make the discovered file look valid, or a working file look invalid.
_CHECK_ENV_KEYS = (
    "LANG",
    "LC_ALL",
    "TERM",
    "SSL_CERT_FILE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "ALL_PROXY",
)


@dataclass(frozen=True)
class CredentialScan:
    """Discovery result for one selected provider. Metadata only — a scan
    never carries credential material."""

    provider: str
    status: str  # DISCOVERY_STATUSES + "skipped"
    detail: str
    hint: str | None = None
    path: str = ""  # display path (``~/``-relative)
    login: str = ""  # official login guidance for remediation

    @property
    def ok(self) -> bool:
        return self.status in ("discovered", "verified")

    def to_check(self) -> Check:
        """Advisory ``Check`` for ``sbx init`` / ``sbx doctor`` output.

        Discovery findings are always warn-level: a missing local credential
        is guidance for the user, not a broken deployment.
        """
        return Check(
            name=f"cred:{self.provider}",
            ok=self.ok,
            warn=not self.ok,
            detail=self.detail,
            hint=self.hint,
        )


def login_hint(provider: str) -> str:
    """Official auth entry point for ``provider`` (never guesses paths)."""
    return LOGIN_HINTS.get(provider, f"log in with the {provider} CLI")


def _import_hint(provider: str, relpath: str, home: Path) -> str:
    """How a discovered credential reaches the deployment."""
    path = home / relpath
    onboard = f"`python -m control.onboarding --modal import --provider {provider} --from {path}`"
    if provider == "codex":
        return (
            "import it with `modal secret create sbx-codex-auth "
            f'CODEX_AUTH_JSON="$(cat {path})"` or {onboard}'
        )
    return f"import it with {onboard}"


def _file_status(
    desc: Any, relpath: str, home: Path, allow_open_permissions: bool
) -> tuple[str, str]:
    """``(status, why)`` for one declared file — ``why`` never contains content."""
    path = home / relpath
    if path.is_symlink():
        return "schema_invalid", "is a symlink — credential imports refuse symlinks"
    if not path.exists():
        return "not_found", "missing"
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        return "permission_invalid", f"cannot stat it ({exc.strerror or exc})"
    if not stat.S_ISREG(mode):
        return "schema_invalid", "not a regular file"
    if stat.S_IMODE(mode) & 0o077 and not allow_open_permissions:
        return (
            "permission_invalid",
            f"mode {oct(stat.S_IMODE(mode))} is group/other accessible",
        )
    try:
        content = path.read_bytes()
    except OSError as exc:
        return "permission_invalid", f"not readable ({exc.strerror or exc})"
    try:
        _check_content_schema(desc, relpath, content)
    except OnboardingError as exc:
        return "schema_invalid", str(exc)
    return "discovered", f"mode {oct(stat.S_IMODE(mode))}, schema ok"


def scan_provider(
    provider: str,
    *,
    home: Path,
    env: Mapping[str, str] | None = None,
    allow_open_permissions: bool = False,
    auth_check: Callable[[str, Path], str] | None = None,
) -> CredentialScan:
    """Scan one provider's declared credential files under ``home``.

    ``auth_check(provider, home)`` may return ``ok`` / ``auth_invalid`` /
    ``probe_unavailable`` / ``cli_missing`` — it runs only on a
    schema-clean file.
    """
    try:
        desc = descriptor_for(provider)
    except OnboardingError:
        return CredentialScan(
            provider=provider,
            status="skipped",
            detail="not a supported provider — nothing to scan",
        )
    login = login_hint(provider)

    worst: tuple[int, str, str, str] | None = None  # severity, status, why, relpath
    for relpath in desc.credential_files:
        status, why = _file_status(desc, relpath, home, allow_open_permissions)
        rank = _SEVERITY[status]
        if worst is None or rank > worst[0]:
            worst = (rank, status, why, relpath)

    if worst is None:  # descriptor declares no files
        return CredentialScan(
            provider=provider,
            status="skipped",
            detail="no declared credential files — nothing to scan",
            login=login,
        )

    _rank, status, why, relpath = worst
    shown = f"~/{relpath}"

    if status == "not_found":
        return CredentialScan(
            provider=provider,
            status=status,
            detail=f"no credential at {shown}",
            hint=f"{login} (writes {shown}), then `sbx deploy`",
            path=shown,
            login=login,
        )
    if status == "permission_invalid":
        path = home / relpath
        if "mode" in why:
            hint = (
                f"run `chmod 600 {path}` so only you can read it "
                "(imports refuse group/other-accessible credentials), or pass "
                "--allow-open-permissions"
            )
        else:
            hint = f"make {path} owner-readable, or {login} to recreate it"
        return CredentialScan(
            provider=provider,
            status=status,
            detail=f"{shown}: {why}",
            hint=hint,
            path=shown,
            login=login,
        )
    if status == "schema_invalid":
        return CredentialScan(
            provider=provider,
            status=status,
            detail=f"{shown}: {why}",
            hint=f"{login} to regenerate a valid credential",
            path=shown,
            login=login,
        )

    # All declared files present, private, schema-clean.
    detail = f"{shown} present ({why})"
    if auth_check is not None:
        try:
            outcome = auth_check(provider, home)
        except Exception:
            outcome = "probe_unavailable"
        if outcome == "ok":
            return CredentialScan(
                provider=provider,
                status="verified",
                detail=f"{detail}; provider auth check passed",
                path=shown,
                login=login,
            )
        if outcome == "auth_invalid":
            return CredentialScan(
                provider=provider,
                status="auth_invalid",
                detail=f"{detail}; provider rejected the credential",
                hint=f"{login} to re-authenticate, then re-verify",
                path=shown,
                login=login,
            )
        if outcome == "cli_missing":
            detail += "; auth check skipped (provider CLI not on PATH)"
        else:
            detail += "; auth check inconclusive"
    return CredentialScan(
        provider=provider,
        status="discovered",
        detail=detail,
        hint=_import_hint(provider, relpath, home),
        path=shown,
        login=login,
    )


def _home_from(env: Mapping[str, str]) -> Path:
    raw = env.get("HOME") or os.environ.get("HOME")
    return Path(raw) if raw else Path.home()


def scan_credentials(
    providers: Iterable[str],
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    allow_open_permissions: bool = False,
    auth_check: Callable[[str, Path], str] | None = None,
) -> list[CredentialScan]:
    """Scan every selected provider. Unselected providers are not scanned —
    they must not block onboarding."""
    env = os.environ if env is None else env
    home = home or _home_from(env)
    return [
        scan_provider(
            provider,
            home=home,
            env=env,
            allow_open_permissions=allow_open_permissions,
            auth_check=auth_check,
        )
        for provider in providers
    ]


def cli_auth_check(
    provider: str,
    home: Path,
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 30.0,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """Run the provider CLI's own auth check against ``home`` on the host.

    Returns ``ok`` / ``auth_invalid`` / ``probe_unavailable`` /
    ``cli_missing``. The child env is scrubbed to HOME/PATH/locale/proxy so
    the check reflects the discovered file, not ambient credential vars.
    """
    parent = os.environ if env is None else env
    argv = provider_auth_argv(provider, env=parent)
    if argv is None:
        return "probe_unavailable"
    child_env = {
        "HOME": str(home),
        "PATH": parent.get("PATH") or os.defpath,
    }
    for key in _CHECK_ENV_KEYS:
        if parent.get(key):
            child_env[key] = parent[key]
    try:
        proc = runner(
            argv,
            capture_output=True,
            text=True,
            env=child_env,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return "cli_missing"
    except (OSError, subprocess.TimeoutExpired):
        return "probe_unavailable"
    output = (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
    return classify_auth_output(provider, getattr(proc, "returncode", -1), output)
