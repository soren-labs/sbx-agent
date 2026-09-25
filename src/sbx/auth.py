"""``sbx auth`` — canonical provider auth for the CLI (SOR-213/SOR-216).

Thin shell over ``control.provider_auth.AuthService``: the adapter/service
in ``control/`` are the single source of truth — this module only wires
the deployment-specific seams (env-selected account store, managed-Secret
writer, remote ``/v1`` verify, the official-CLI login runner).

Auth model:

* ``login`` / ``relogin`` run the provider's *own* CLI/OAuth/device flow —
  no token or JSON paste is ever required. The CLI writes its credential
  files under ``$HOME`` and the adapter captures/normalizes them.
* ``import-existing`` captures the files a previous vendor login already
  wrote (or an explicit ``--from`` path).
* Every write lands as ``unverified`` — the account leaves the scheduler
  pool until a cloud verify (remote ``/v1`` probe when the deployment's
  shared store is in use, else the local sandbox auth probe) promotes it.
* ``logout`` drops credential material (blob + managed Secret +
  lifecycle record, plus local files on request) and leaves the account
  ``unverified``, immediately ineligible.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from control.accounts import PersistentAccountRegistry, select_store
from control.onboarding import OnboardingError, OnboardingService, default_auth_probe
from control.provider_auth import AUTH_SESSION_STATES, AuthService, supported_auth_providers

from sbx.errors import BootstrapError


def _backend_name(env: Mapping[str, str]) -> str:
    return (env.get("SBX_BACKEND") or "local").strip() or "local"


def make_registry(env: Mapping[str, str]) -> PersistentAccountRegistry:
    """The account registry for this CLI context (env-selected store)."""
    backend = _backend_name(env)
    return PersistentAccountRegistry(select_store(backend=backend))


def make_secret_writer(env: Mapping[str, str], writer: Any = None) -> Any:
    """Managed-Secrecy writer: the modal writer when the modal store is in
    use, else ``None`` (file store — secrets materialize at deploy)."""
    if writer is not None:
        return writer
    if _backend_name(env) == "modal":
        from control.credsync import ModalCredentialSecretWriter

        return ModalCredentialSecretWriter()
    return None


def make_auth_service(
    env: Mapping[str, str],
    *,
    probe: Any = None,
    secret_writer: Any = None,
    login_runner: Callable[..., int] | None = None,
    registry: PersistentAccountRegistry | None = None,
) -> AuthService:
    """The canonical AuthService bound to this CLI's store/seams."""
    reg = registry or make_registry(env)
    onboarding = OnboardingService(
        reg,
        probe=probe if probe is not None else default_auth_probe(bin_env=env),
        secret_writer=make_secret_writer(env, secret_writer),
    )
    return AuthService(
        reg,
        onboarding=onboarding,
        home=Path(env.get("HOME") or str(Path.home())),
        env=env,
        login_runner=login_runner,
    )


def remote_client(cfg: Any, env: Mapping[str, str], *, transport: Any = None) -> Any | None:
    """A ``V1Client`` for the deployed gate — only when the shared cloud
    store is in use (``SBX_BACKEND=modal``) and a base URL + key resolve."""
    if _backend_name(env) != "modal":
        return None
    from sbx.deploy import read_deploy_state
    from sbx.httpapi import V1Client
    from sbx.keys import resolve_api_key

    base_url = cfg.config.api_base_url or read_deploy_state(env).get("app_url") or ""
    token = resolve_api_key(env)
    if not base_url or not token:
        return None
    return V1Client(base_url, token, transport=transport, timeout=30.0)


def _verify_remote(client: Any, account_id: str) -> dict[str, Any] | None:
    """POST ``/v1/accounts/{id}/verify`` — the cloud auth probe (SOR-213)."""
    try:
        payload = client.verify_account(account_id)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def verify_account(
    service: AuthService,
    account_id: str,
    *,
    client: Any = None,
) -> dict[str, Any]:
    """Verify via the remote probe when configured, else the local one."""
    if client is not None:
        payload = _verify_remote(client, account_id)
        if payload is not None:
            # The remote registry is the same store — the session view
            # reflects the verify it just ran.
            return {
                "account_id": account_id,
                "lane": "remote",
                "probe": "ok" if payload.get("status") == "active" else payload.get("status"),
                "verified": payload.get("status") == "active",
                "session": service.session(account_id).to_dict(),
            }
    outcome = service.verify(account_id)
    outcome["lane"] = "local"
    return outcome


def _targets(service: AuthService, args: Any) -> list[str]:
    """``--account-id`` or ``--provider`` → the account ids to act on."""
    account_id = getattr(args, "account_id", None)
    provider = getattr(args, "provider", None)
    if account_id:
        return [account_id]
    if provider:
        ids = [s.account_id for s in service.sessions(provider)]
        if ids:
            return ids
        raise BootstrapError(
            f"no {provider} accounts — run `sbx auth import-existing` first",
            code="no_accounts",
        )
    raise BootstrapError("pass --account-id or --provider", code="missing_target")


def auth_status(
    service: AuthService, env: Mapping[str, str], *, provider: str | None = None
) -> dict:
    """Combined local-credential scan + cloud account auth-state payload."""
    from sbx.credentials import scan_credentials

    providers = (provider,) if provider else tuple(supported_auth_providers())
    local = scan_credentials(providers=providers, env=env)
    return {
        "auth_states": list(AUTH_SESSION_STATES),
        "local": [
            {
                "provider": s.provider,
                "status": s.status,
                "path": s.path,
                "detail": s.detail,
                "hint": s.hint,
                "login": s.login,
            }
            for s in local
        ],
        "accounts": service.status(provider),
    }


def auth_login(
    service: AuthService,
    env: Mapping[str, str],
    *,
    provider: str,
    account_id: str | None = None,
    label: str = "",
    slots: int = 1,
    models: tuple[str, ...] | None = None,
    no_verify: bool = False,
    experimental_ok: bool = False,
    allow_open_permissions: bool = False,
    login_runner: Callable[..., int] | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Run the provider's official login flow, capture, import, verify."""
    try:
        outcome = service.login(
            provider,
            account_id=account_id,
            label=label,
            slots=slots,
            models=models,
            allow_open_permissions=allow_open_permissions,
            experimental_ok=experimental_ok,
            # A configured remote client verifies once below — never run
            # the local probe in addition to it.
            verify=not no_verify and client is None,
            runner=login_runner,
        )
    except OnboardingError as exc:
        raise BootstrapError(str(exc), code=exc.code) from exc
    sid = outcome["account_id"]
    if not no_verify and client is not None:
        outcome.update(verify_account(service, sid, client=client))
    return outcome


def auth_import_existing(
    service: AuthService,
    env: Mapping[str, str],
    *,
    provider: str,
    source: Path | str | None = None,
    account_id: str | None = None,
    label: str = "",
    slots: int = 1,
    models: tuple[str, ...] | None = None,
    no_verify: bool = False,
    experimental_ok: bool = False,
    allow_open_permissions: bool = False,
    client: Any = None,
) -> dict[str, Any]:
    """Capture the files a vendor login already wrote; never a token paste."""
    try:
        outcome = service.import_existing(
            provider,
            source=source,
            account_id=account_id,
            label=label,
            slots=slots,
            models=models,
            experimental_ok=experimental_ok,
            allow_open_permissions=allow_open_permissions,
            verify=not no_verify and client is None,
        )
    except OnboardingError as exc:
        raise BootstrapError(str(exc), code=exc.code) from exc
    sid = outcome["account_id"]
    if not no_verify and client is not None:
        outcome.update(verify_account(service, sid, client=client))
    return outcome


def auth_verify(
    service: AuthService,
    env: Mapping[str, str],
    *,
    args: Any,
    client: Any = None,
) -> dict[str, Any]:
    """Verify one or more accounts (remote probe first when configured)."""
    results = []
    for account_id in _targets(service, args):
        try:
            results.append(verify_account(service, account_id, client=client))
        except OnboardingError as exc:
            raise BootstrapError(str(exc), code=exc.code) from exc
    return {"accounts": results, "verified": all(r.get("verified") for r in results)}


def auth_relink(
    service: AuthService,
    env: Mapping[str, str],
    *,
    args: Any,
    login_runner: Callable[..., int] | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Re-capture → refresh → verify; restores scheduler eligibility."""
    source = getattr(args, "source", None)
    relogin = bool(getattr(args, "relogin", False))
    no_verify = bool(getattr(args, "no_verify", False))
    allow_open = bool(getattr(args, "allow_open_permissions", False))
    results = []
    for account_id in _targets(service, args):
        try:
            outcome = service.relink(
                account_id,
                source=source,
                verify=not no_verify and client is None,
                allow_open_permissions=allow_open,
                runner=login_runner,
                relogin=relogin,
            )
        except OnboardingError as exc:
            raise BootstrapError(str(exc), code=exc.code) from exc
        if not no_verify and client is not None:
            outcome.update(verify_account(service, account_id, client=client))
        results.append(outcome)
    return {"accounts": results, "verified": all(r.get("verified") for r in results)}


def auth_logout(
    service: AuthService,
    env: Mapping[str, str],
    *,
    args: Any,
    plane: Any = None,
) -> dict[str, Any]:
    """Sign out: drop credential material, flip to ``unverified``.

    Removes the stored blob + lifecycle record and the managed
    ``sbx-acct-<id>`` Secret (``--keep-secret`` preserves it);
    ``--local`` also deletes the provider's credential files under HOME.
    """
    remove_local = bool(getattr(args, "local", False))
    keep_secret = bool(getattr(args, "keep_secret", False))
    deleter = getattr(plane, "delete_secret", None) if plane is not None else None
    results = []
    for account_id in _targets(service, args):
        try:
            results.append(
                service.logout(
                    account_id,
                    remove_local=remove_local,
                    delete_secret=not keep_secret,
                    secret_deleter=deleter,
                )
            )
        except OnboardingError as exc:
            raise BootstrapError(str(exc), code=exc.code) from exc
    return {"accounts": results}


__all__ = [
    "auth_import_existing",
    "auth_login",
    "auth_logout",
    "auth_relink",
    "auth_status",
    "auth_verify",
    "make_auth_service",
    "make_registry",
    "remote_client",
]
