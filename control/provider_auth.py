"""SOR-213/SOR-216: canonical provider auth — adapter, service, session states.

One auth engine shared by the ``sbx auth`` CLI surface and the control
plane: a :class:`ProviderAuthAdapter` per provider knows the *official*
vendor CLI login flow, the declared credential files, capture/normalize
rules, and the auth-check argv; :class:`AuthService` drives the account
lifecycle over the registry:

    login / import-existing   capture local credential files written by the
                              official CLI (never a token/JSON paste)
                              → validate/normalize → account record +
                              credential blob (+ managed ``sbx-acct-<id>``
                              Secret when a writer is configured)
    verify                    cloud verify probe → ``verified_at`` lifecycle
                              evidence → ``active`` (scheduler-eligible)
    relink                    re-capture → refresh → verify → eligibility
                              restored
    logout                    drop credential material (blob + managed
                              Secret + lifecycle record) → ``unverified``

``AUTH_SESSION_STATES`` is the canonical per-account auth-session
vocabulary surfaced by ``sbx auth status`` and ``GET /v1/accounts``:

* ``unauthenticated`` — nothing materialized (never linked, or logged out)
* ``authenticating``  — the official login flow is running (transient,
  never persisted)
* ``authenticated``   — local credential files exist, not yet imported
* ``materialized``    — blob stored (account ``unverified``) but never
  proven by the cloud verify probe
* ``verified``        — ``active`` with live ``verified_at`` evidence;
  the only scheduler-eligible state
* ``reauth_required`` — provider rejected the grant (``invalid`` account
  or terminal lifecycle state); ``relink`` is the way out
* ``unhealthy``       — ``cooling``/refresh-error: temporarily ineligible
* ``disabled``        — operator-disabled

Accounts become scheduler-eligible only after credential materialization
+ cloud verify; ``reauth_required``/``unhealthy``/``disabled`` (and
``unverified``) are never picked.
"""

from __future__ import annotations

import dataclasses
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from control.accounts import PersistentAccountRegistry, is_valid_account_id
from control.config import account_secret_prefix
from control.onboarding import (
    _PROVIDER_BINS,
    OnboardingError,
    OnboardingService,
    collect_credential_blob,
    descriptor_for,
    provider_auth_argv,
)
from control.ports import Account

# Canonical auth-session states (see module docstring).
AUTH_SESSION_STATES: tuple[str, ...] = (
    "unauthenticated",
    "authenticating",
    "authenticated",
    "materialized",
    "verified",
    "reauth_required",
    "unhealthy",
    "disabled",
)

# Official vendor login entry points — argv tail appended to the provider
# binary. These are the *only* supported auth flows: the vendor CLI's own
# interactive/OAuth/device flow writes the credential files; the harness
# never asks for a token or JSON blob (SOR-213).
PROVIDER_LOGIN_ARGV: dict[str, tuple[str, ...]] = {
    "codex": ("login",),
    "devin": (),  # bare `devin` runs the interactive login
    "antigravity": (),  # bare `agy` runs the OAuth login
    "grok": (),  # bare `grok` runs its login
    "opencode": ("auth", "login"),
}

# Host-side env keys kept for the login/auth-check child processes —
# ambient credential variables are scrubbed so the vendor CLI only sees
# the filesystem credential under ``HOME`` (mirrors ``provider_cli_env``).
_HOST_ENV_KEYS = (
    "HOME",
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


def _login_tail(provider: str) -> tuple[str, ...] | None:
    """The official login argv tail; None for providers without one."""
    return PROVIDER_LOGIN_ARGV.get(provider)


def provider_login_argv(provider: str, env: Mapping[str, str] | None = None) -> list[str] | None:
    """Argv for the provider's own login flow; None when unsupported."""
    tail = _login_tail(provider)
    if tail is None:
        return None
    env = os.environ if env is None else env
    bin_env, default_bin = _PROVIDER_BINS.get(provider, ("", provider))
    tokens = shlex.split(env.get(bin_env) or default_bin)
    if not tokens:
        return None
    if len(tokens) == 1 and tokens[0].endswith(".py"):
        return [sys.executable, tokens[0], *tail]
    return [*tokens, *tail]


def login_hint(provider: str) -> str:
    """Human-readable official login instruction for ``provider``."""
    argv = provider_login_argv(provider, env={})
    if argv:
        if argv[0] == sys.executable:
            argv = argv[1:]
        return "run `" + " ".join(argv) + "`"
    return f"log in with the {provider} CLI"


def host_cli_env(
    provider: str, home: Path, parent: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Env for a host-side provider CLI run (login or auth check).

    Only ``HOME``/``PATH``/locale/proxy survive — ambient credential env
    vars are scrubbed so the CLI's view comes from the files under ``home``
    alone. Devin/OpenCode pin XDG dirs under ``home`` (same pinning as
    ``provider_cli_env``) so the credential lands/reads at the declared
    relpaths.
    """
    parent = os.environ if parent is None else parent
    env = {k: v for k, v in parent.items() if k in _HOST_ENV_KEYS}
    env["HOME"] = str(home)
    env["PATH"] = parent.get("PATH") or os.defpath
    if provider in ("devin", "opencode"):
        env.update(
            {
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_STATE_HOME": str(home / ".local" / "state"),
            }
        )
    return env


@dataclass(frozen=True)
class ProviderAuthAdapter:
    """Canonical auth adapter for one provider (official-CLI flows only).

    Knows how to ``capture`` the credential files the vendor CLI wrote,
    the auth-check argv that proves them, and the declared relpaths —
    the single source of truth ``sbx auth`` and the backend share.
    """

    descriptor: Any

    @property
    def provider(self) -> str:
        return self.descriptor.provider

    @property
    def credential_files(self) -> tuple[str, ...]:
        return tuple(self.descriptor.credential_files)

    def login_argv(self, env: Mapping[str, str] | None = None) -> list[str] | None:
        return provider_login_argv(self.provider, env=env)

    def auth_check_argv(self, env: Mapping[str, str] | None = None) -> list[str] | None:
        return provider_auth_argv(self.provider, env=env)

    def local_credential_paths(self, home: Path) -> list[Path]:
        """Declared credential files under ``home`` (existing or not)."""
        return [Path(home) / rel for rel in self.descriptor.credential_files]

    def existing_local_files(self, home: Path) -> list[Path]:
        """Declared credential files that currently exist under ``home``."""
        return [p for p in self.local_credential_paths(home) if p.exists() or p.is_symlink()]

    def capture(self, home: Path | str, *, allow_open_permissions: bool = False) -> dict[str, Any]:
        """Capture + normalize the credential files under ``home``.

        Reads exactly the declared relpaths and returns the canonical
        ``{"provider": P, "files": {relpath: content}}`` blob — the same
        shape ``runner init`` consumes and the store keeps. Never returns
        anything outside the declared set.
        """
        return collect_credential_blob(
            self.provider,
            Path(home),
            allow_open_permissions=allow_open_permissions,
        )

    def normalize(self, blob: dict[str, Any]) -> dict[str, Any]:
        """Validate ``blob`` against the provider's declared schema."""
        from control.onboarding import validate_credential_blob

        return validate_credential_blob(self.provider, blob)


def adapter_for(provider: str) -> ProviderAuthAdapter:
    """The canonical adapter for ``provider``; raises OnboardingError."""
    return ProviderAuthAdapter(descriptor_for(provider))


def supported_auth_providers() -> list[str]:
    """Providers with an official auth/login flow the adapter supports."""
    return sorted(PROVIDER_LOGIN_ARGV)


def _run_login_default(argv: list[str], env: Mapping[str, str]) -> int:
    """Run the vendor CLI login interactively (inherits stdio)."""
    try:
        proc = subprocess.run(list(argv), env=dict(env), check=False)
    except FileNotFoundError as exc:
        raise OnboardingError(
            "cli_missing",
            f"provider CLI {argv[0]!r} not found on PATH — install it, then retry",
        ) from exc
    except OSError as exc:
        raise OnboardingError("cli_missing", f"cannot run {argv[0]!r}: {exc}") from exc
    return int(proc.returncode)


@dataclass(frozen=True)
class AuthSession:
    """Canonical auth-session view of one account — metadata only.

    Never carries credential material: ``verified_at``/``lifecycle_state``
    are the only proof fields; the blob itself stays in the store lane.
    """

    account_id: str
    provider: str
    label: str
    status: str
    auth_state: str
    has_credential: bool
    verified_at: int | None
    lifecycle_state: str | None
    secret_name: str
    running: int
    max_concurrent: int
    models: tuple[str, ...]
    created_at: str
    last_used_at: str | None
    cooldown_until: str | None
    last_error: str | None

    @property
    def schedulable(self) -> bool:
        return self.status == "active" and self.auth_state == "verified"

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "provider": self.provider,
            "label": self.label,
            "status": self.status,
            "auth_state": self.auth_state,
            "has_credential": self.has_credential,
            "verified_at": self.verified_at,
            "lifecycle_state": self.lifecycle_state,
            "secret_name": self.secret_name,
            "running": self.running,
            "max_concurrent": self.max_concurrent,
            "models": list(self.models),
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "cooldown_until": self.cooldown_until,
            "last_error": self.last_error,
            "schedulable": self.schedulable,
        }


def auth_state_for(
    account: Any,
    lifecycle_record: Mapping[str, Any] | None,
    has_credential: bool,
) -> str:
    """Derive the canonical auth-session state for an account record.

    Reads only non-secret metadata: account ``status`` plus the lifecycle
    record's ``state``/``verified_at``/``last_error`` fields.
    """
    status = getattr(account, "status", "") or ""
    if status == "disabled":
        return "disabled"
    rec = lifecycle_record or {}
    lifecycle_state = rec.get("state")
    if lifecycle_state in ("reauth_required", "revoked"):
        return "reauth_required"
    if status == "invalid":
        return "reauth_required"
    if status == "cooling":
        return "unhealthy"
    if status == "active" and rec.get("verified_at"):
        return "verified"
    if has_credential or getattr(account, "secret_name", ""):
        return "materialized"
    return "unauthenticated"


class AuthService:
    """Canonical auth engine over a ``PersistentAccountRegistry``.

    ``onboarding`` supplies the store/verify/materialization seams
    (injectable for tests); ``lifecycle`` defaults to the registry's
    credential-lifecycle lane (SOR-176 — reused, never duplicated).
    """

    def __init__(
        self,
        registry: PersistentAccountRegistry,
        *,
        onboarding: OnboardingService | None = None,
        home: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        login_runner: Callable[..., int] | None = None,
    ) -> None:
        self._registry = registry
        self._onboarding = onboarding or OnboardingService(registry)
        self._home = (
            Path(home)
            if home is not None
            else Path((env or os.environ).get("HOME") or str(Path.home()))
        )
        self._env = dict(env if env is not None else os.environ)
        self._login_runner = login_runner

    @property
    def registry(self) -> PersistentAccountRegistry:
        return self._registry

    @property
    def home(self) -> Path:
        return self._home

    def adapter(self, provider: str) -> ProviderAuthAdapter:
        return adapter_for(provider)

    def _lifecycle(self) -> Any:
        from control.credlifecycle import CredentialLifecycleService

        return CredentialLifecycleService(self._registry)

    # ------------------------------------------------------------- reads

    def session(self, account_id: str) -> AuthSession:
        """Canonical auth-session view for one account."""
        account = self._registry.get(account_id)
        if account is None:
            raise OnboardingError("account_not_found", f"account {account_id!r} not found")
        return self._session_for(account)

    def sessions(self, provider: str | None = None) -> list[AuthSession]:
        return [self._session_for(a) for a in self._registry.list(provider)]

    def _session_for(self, account: Account) -> AuthSession:
        if not is_valid_account_id(account.id):
            # A corrupt/non-conformant record never reaches the store lanes.
            return AuthSession(
                account_id=account.id,
                provider=account.provider,
                label=account.label,
                status=account.status,
                auth_state="disabled" if account.status == "disabled" else "unhealthy",
                has_credential=False,
                verified_at=None,
                lifecycle_state=None,
                secret_name=account.secret_name,
                running=0,
                max_concurrent=account.max_concurrent,
                models=account.models,
                created_at=account.created_at,
                last_used_at=account.last_used_at,
                cooldown_until=account.cooldown_until,
                last_error=account.last_error,
            )
        try:
            blob = self._registry.get_credential_blob(account.id)
        except Exception:
            blob = None
        try:
            rec = self._registry.get_credential_lifecycle(account.id)
        except Exception:
            rec = None
        return AuthSession(
            account_id=account.id,
            provider=account.provider,
            label=account.label,
            status=account.status,
            auth_state=auth_state_for(account, rec, blob is not None),
            has_credential=blob is not None,
            verified_at=(rec or {}).get("verified_at"),
            lifecycle_state=(rec or {}).get("state"),
            secret_name=account.secret_name,
            running=self._registry.running_count(account.id),
            max_concurrent=account.max_concurrent,
            models=account.models,
            created_at=account.created_at,
            last_used_at=account.last_used_at,
            cooldown_until=account.cooldown_until,
            last_error=account.last_error,
        )

    def status(self, provider: str | None = None) -> list[dict[str, Any]]:
        """``sbx auth status`` payload: one canonical session per account."""
        return [s.to_dict() for s in self.sessions(provider)]

    # ----------------------------------------------------------- materialize

    def capture(
        self,
        provider: str,
        *,
        home: Path | str | None = None,
        source: Path | str | None = None,
        allow_open_permissions: bool = False,
    ) -> dict[str, Any]:
        """Capture the provider's credential files into a canonical blob.

        ``source`` may point at a file/dir/blob; otherwise the declared
        files under ``home`` (default: the session's home) are captured.
        """
        adapter = self.adapter(provider)
        if source is not None:
            return collect_credential_blob(
                provider, source, allow_open_permissions=allow_open_permissions
            )
        return adapter.capture(
            home if home is not None else self._home,
            allow_open_permissions=allow_open_permissions,
        )

    def import_existing(
        self,
        provider: str,
        *,
        account_id: str | None = None,
        label: str = "",
        slots: int = 1,
        models: tuple[str, ...] | list[str] | None = None,
        home: Path | str | None = None,
        source: Path | str | None = None,
        experimental_ok: bool = False,
        allow_open_permissions: bool = False,
        verify: bool = True,
    ) -> dict[str, Any]:
        """Import the captured credential as a new ``unverified`` account.

        Materializes the blob (and the managed ``sbx-acct-<id>`` Secret
        when the onboarding service has a writer), then — unless
        ``verify=False`` — runs the configured cloud verify probe, which
        promotes the account to ``active`` on a pass. The account is
        never schedulable before that pass (SOR-216).
        """
        # Resolve the capture source first so a failed capture never
        # leaves a half-created account.
        self.adapter(provider)
        if source is None:
            self.capture(
                provider, home=home, allow_open_permissions=allow_open_permissions
            )  # validates the materialized set exists
            src: Path | str = Path(home) if home is not None else self._home
        else:
            src = source
        account = self._onboarding.add(
            provider,
            src,
            label=label,
            account_id=account_id,
            slots=slots,
            models=models,
            experimental_ok=experimental_ok,
            allow_open_permissions=allow_open_permissions,
        )
        outcome: dict[str, Any] = {
            "account_id": account.id,
            "provider": account.provider,
            "created": True,
            "verified": False,
        }
        if verify:
            result, updated = self._onboarding.verify(account.id)
            outcome["probe"] = result.status
            outcome["verified"] = result.status == "ok" and updated.status == "active"
        outcome["session"] = self.session(account.id).to_dict()
        return outcome

    def verify(self, account_id: str) -> dict[str, Any]:
        """Run the configured cloud verify probe; promote on a pass."""
        result, account = self._onboarding.verify(account_id)
        return {
            "account_id": account.id,
            "probe": result.status,
            "detail": result.detail,
            "verified": result.status == "ok" and account.status == "active",
            "session": self.session(account.id).to_dict(),
        }

    # --------------------------------------------------------------- login

    def login(
        self,
        provider: str,
        *,
        account_id: str | None = None,
        label: str = "",
        slots: int = 1,
        models: tuple[str, ...] | list[str] | None = None,
        home: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        allow_open_permissions: bool = False,
        experimental_ok: bool = False,
        verify: bool = True,
        runner: Callable[..., int] | None = None,
    ) -> dict[str, Any]:
        """Run the provider's *official* login flow, then import + verify.

        The vendor CLI itself does the OAuth/device/interactive work and
        writes its credential files under ``home``; afterwards they are
        captured, imported as (or relinked into) the account, and verified.
        A token/JSON paste is never required (SOR-213).
        """
        adapter = self.adapter(provider)
        home_path = Path(home) if home is not None else self._home
        argv = adapter.login_argv(env=env if env is not None else self._env)
        if argv is None:
            raise OnboardingError(
                "no_login_flow", f"provider {provider!r} has no supported login flow"
            )
        run = runner or self._login_runner or _run_login_default
        rc = run(argv, env=host_cli_env(provider, home_path, self._env))
        if rc != 0:
            raise OnboardingError(
                "login_failed", f"{provider} login exited {rc} — nothing captured"
            )
        if account_id is not None and self._registry.get(account_id) is not None:
            # Existing account → relink: re-capture + refresh + verify.
            outcome = self.relink(
                account_id,
                home=home_path,
                verify=verify,
                allow_open_permissions=allow_open_permissions,
            )
            outcome["login"] = True
            return outcome
        return self.import_existing(
            provider,
            account_id=account_id,
            label=label,
            slots=slots,
            models=models,
            home=home_path,
            experimental_ok=experimental_ok,
            allow_open_permissions=allow_open_permissions,
            verify=verify,
        )

    def relink(
        self,
        account_id: str,
        *,
        home: Path | str | None = None,
        source: Path | str | None = None,
        verify: bool = True,
        allow_open_permissions: bool = False,
        env: Mapping[str, str] | None = None,
        runner: Callable[..., int] | None = None,
        relogin: bool = False,
    ) -> dict[str, Any]:
        """Re-capture the credential, refresh the store, re-verify.

        Restores scheduler eligibility after ``reauth_required``/rotation
        or when the cloud copy went stale. ``relogin=True`` runs the
        official login flow first — for a grant the provider already
        killed (a stale local file cannot be re-captured into a live one).
        """
        account = self._registry.get(account_id)
        if account is None:
            raise OnboardingError("account_not_found", f"account {account_id!r} not found")
        provider = account.provider
        home_path = Path(home) if home is not None else self._home
        if relogin:
            argv = self.adapter(provider).login_argv(env=env if env is not None else self._env)
            if argv is None:
                raise OnboardingError(
                    "no_login_flow", f"provider {provider!r} has no supported login flow"
                )
            run = runner or self._login_runner or _run_login_default
            rc = run(argv, env=host_cli_env(provider, home_path, self._env))
            if rc != 0:
                raise OnboardingError(
                    "login_failed", f"{provider} login exited {rc} — nothing captured"
                )
        src: Path | str = source if source is not None else home_path
        outcome = self._onboarding.refresh(
            account_id, src, allow_open_permissions=allow_open_permissions
        )
        result: dict[str, Any] = {
            "account_id": account_id,
            "provider": provider,
            "created": False,
            "changed": outcome["changed"],
            "secret": outcome["secret"],
            "verified": False,
        }
        if verify:
            probe_result, updated = self._onboarding.verify(account_id)
            result["probe"] = probe_result.status
            result["verified"] = probe_result.status == "ok" and updated.status == "active"
        result["session"] = self.session(account_id).to_dict()
        return result

    # --------------------------------------------------------------- logout

    def logout(
        self,
        account_id: str,
        *,
        remove_local: bool = False,
        delete_secret: bool = True,
        home: Path | str | None = None,
        secret_deleter: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        """Sign the account out: drop credential material, keep the record.

        The account flips to ``unverified`` (immediately ineligible), the
        stored blob + lifecycle record are cleared, and the managed
        ``sbx-acct-<id>`` Secret is deleted when one was materialized.
        ``remove_local`` additionally deletes the provider's credential
        files under ``home``; a custom ``secret_name`` (externally managed)
        Secret is never touched.
        """
        account = self._registry.get(account_id)
        if account is None:
            raise OnboardingError("account_not_found", f"account {account_id!r} not found")
        removed: list[str] = []
        # Store lanes: blob + lifecycle record.
        try:
            self._registry.store.delete_blob(account_id)
        except Exception:
            pass
        lifecycle_store = getattr(self._registry.store, "delete_lifecycle", None)
        if callable(lifecycle_store):
            try:
                lifecycle_store(account_id)
            except Exception:
                pass
        else:
            self._lifecycle().note_logged_out(account_id)
        # Managed Secret — only the ``<prefix><id>`` lane is ever deleted.
        managed = f"{account_secret_prefix()}{account_id}"
        cleared_secret_name = False
        if delete_secret and account.secret_name == managed:
            try:
                if secret_deleter is not None and secret_deleter(managed):
                    removed.append(managed)
                    cleared_secret_name = True
                elif secret_deleter is not None:
                    # Confirmed absent — the record must not keep pointing
                    # at a Secret that no longer holds material.
                    cleared_secret_name = True
            except Exception:
                pass
        # Local credential files (optional).
        if remove_local:
            home_path = Path(home) if home is not None else self._home
            for path in self.adapter(account.provider).local_credential_paths(home_path):
                try:
                    if path.is_symlink() or path.is_file():
                        path.unlink()
                        removed.append(f"~/{path.name}")
                except OSError:
                    pass
        if cleared_secret_name:
            # The managed Secret is gone — the record stops referencing it
            # so the account reads as fully unauthenticated again.
            self._registry.put(dataclasses.replace(account, secret_name=""))
        updated = self._registry.mark_status(account_id, "unverified", last_error=None)
        return {
            "account_id": account_id,
            "provider": account.provider,
            "removed": removed,
            "session": self.session(account_id).to_dict() if updated else None,
        }


__all__ = [
    "AUTH_SESSION_STATES",
    "PROVIDER_LOGIN_ARGV",
    "AuthService",
    "AuthSession",
    "ProviderAuthAdapter",
    "adapter_for",
    "auth_state_for",
    "host_cli_env",
    "login_hint",
    "provider_login_argv",
    "supported_auth_providers",
]
