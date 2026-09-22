"""``sbx deploy`` / ``sbx upgrade`` orchestration (SOR-98).

Deploy is a fixed pipeline of idempotent steps — every step is check-then-
act, so reruns converge instead of duplicating resources, and a failure
reports the completed steps, the failed step, and the remediation hint.
Nothing writes to Modal until the Modal auth check passes, so a failed run
never leaves half-initialized resources behind.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from control.accounts import is_valid_account_id

import sbx
from sbx.config import (
    BootstrapConfig,
    ResolvedConfig,
    basic_auth_path,
    deploy_state_path,
    key_path,
    save,
    state_dir,
    validate_providers,
)
from sbx.credentials import scan_credentials
from sbx.errors import BootstrapError
from sbx.httpapi import ApiError, V1Client
from sbx.keys import fingerprint, load_or_create_key
from sbx.plane import Plane
from sbx.prereqs import check_modal_package, check_python, require


@dataclass(frozen=True)
class StepResult:
    name: str
    changed: bool
    detail: str


@dataclass(frozen=True)
class DeployReport:
    steps: tuple[StepResult, ...]
    base_url: str
    version: str
    key_created: bool
    key_rotated: bool = False
    cli_versions: dict[str, str] | None = None


def app_version() -> str:
    try:
        return importlib.metadata.version("sbx-browser")
    except importlib.metadata.PackageNotFoundError:
        return sbx.__version__


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _write_state(env: Mapping[str, str], payload: dict[str, Any]) -> Path:
    path = deploy_state_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def read_deploy_state(env: Mapping[str, str]) -> dict[str, Any]:
    try:
        data = json.loads(deploy_state_path(env).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_basic_auth(env: Mapping[str, str], user: str, password: str) -> Path:
    path = basic_auth_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # force the mode even when the file already existed
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"user": user, "password": password}) + "\n")
    return path


def _read_basic_auth(env: Mapping[str, str]) -> dict[str, str] | None:
    try:
        data = json.loads(basic_auth_path(env).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("user") and data.get("password"):
        return {"user": str(data["user"]), "password": str(data["password"])}
    return None


def _require_modal_auth(plane: Plane) -> str:
    workspace = plane.workspace()
    if workspace is None:
        raise BootstrapError(
            "not authenticated with Modal",
            hint="run `modal token new` (or `modal setup`), or export "
            "MODAL_TOKEN_ID/MODAL_TOKEN_SECRET, then rerun `sbx deploy`",
            code="modal_auth_missing",
        )
    return workspace


def _ensure_bootstrap_secret(
    cfg: BootstrapConfig, plane: Plane, env: Mapping[str, str]
) -> tuple[StepResult, bool, bool]:
    """Ensure ``sbx-v1-bootstrap`` holds the local key.

    Control only ever sees the token inside the Secret env and stores its
    sha256; when the local key file was just minted while a stale Secret
    exists, the remote value is unrecoverable — rotate it so local and
    remote agree again.
    """
    token, created = load_or_create_key(key_path(env))
    rotated = False
    changed = False
    if created and cfg.bootstrap_secret in plane.list_secret_names():
        plane.delete_secret(cfg.bootstrap_secret)
        rotated = True
    if plane.ensure_secret(cfg.bootstrap_secret, {"SBX_V1_BOOTSTRAP_KEY": token}):
        changed = True
    detail = f"{cfg.bootstrap_secret} seeded ({fingerprint(token)})"
    if rotated:
        detail = f"{cfg.bootstrap_secret} rotated ({fingerprint(token)})"
    return StepResult("secret:bootstrap", changed or rotated, detail), created, rotated


def _ensure_basic_secret(cfg: BootstrapConfig, plane: Plane, env: Mapping[str, str]) -> StepResult:
    """Ensure the internal ``/api/*`` Basic-auth Secret exists.

    Generated credentials are stored locally (0600) for the web board; the
    Secret itself is never echoed.
    """
    if cfg.basic_secret in plane.list_secret_names():
        detail = f"{cfg.basic_secret} present"
        if _read_basic_auth(env) is None:
            detail += " (credentials managed remotely; local copy absent)"
        return StepResult("secret:basic", False, detail)
    creds = _read_basic_auth(env)
    if creds is None:
        creds = {"user": "sbx", "password": secrets.token_urlsafe(24)}
        _write_basic_auth(env, creds["user"], creds["password"])
    plane.ensure_secret(
        cfg.basic_secret,
        {"SBX_BASIC_USER": creds["user"], "SBX_BASIC_PASS": creds["password"]},
    )
    return StepResult("secret:basic", True, f"{cfg.basic_secret} created (local copy saved)")


def _require_codex_secret(cfg: BootstrapConfig, plane: Plane, env: Mapping[str, str]) -> StepResult:
    """Preflight the shared Codex credential Secret.

    Only runs when ``codex`` is a selected provider. When missing, the hint
    reflects the local scan: no ``~/.codex/auth.json`` → ``codex login``
    first; unusable file → its remediation; clean file → the create
    command. Never exposes credential contents (SOR-115).
    """
    if cfg.codex_secret in plane.list_secret_names():
        return StepResult("secret:codex", False, f"{cfg.codex_secret} present")
    scan = scan_credentials(("codex",), env=env)[0]
    create = f'`modal secret create {cfg.codex_secret} CODEX_AUTH_JSON="$(cat ~/.codex/auth.json)"`'
    if scan.ok:
        hint = (
            f"create it with {create}, or set "
            "SBX_CODEX_SECRET_NAME to an existing Secret, then rerun `sbx deploy`"
        )
    else:
        hint = (
            f"{scan.hint}; then create the Secret with {create}, or set "
            "SBX_CODEX_SECRET_NAME to an existing Secret"
        )
    raise BootstrapError(
        f"provider credential Secret {cfg.codex_secret!r} is missing ({scan.detail})",
        hint=hint,
        code="secret_missing",
    )


def _require_github_secret(cfg: BootstrapConfig, existing: set[str]) -> StepResult | None:
    """Fail-before-write check for the optional GitHub bridge Secret (SOR-117).

    Only applies when the resolved config armed the bridge
    (``github.ephemeral`` / ``SBX_GITHUB_EPHEMERAL``) AND named a Secret
    (``github.secret_name`` / ``SBX_GITHUB_SECRET_NAME``) — the named Secret
    holds GH_TOKEN/GITHUB_TOKEN so a *remote* control app sees the token in
    its env and forwards it into sandboxes. A missing named Secret is an
    explicit deploy failure rather than a silently inert bridge.
    """
    name = cfg.github_secret_name
    if not cfg.github_ephemeral or not name:
        return None
    if name in existing:
        return StepResult("secret:github", False, f"{name} present")
    raise BootstrapError(
        f"GitHub bridge Secret {name!r} is missing",
        hint=f"create it with `modal secret create {name} GH_TOKEN=...` "
        "(least privilege: a fine-grained PAT scoped to the agent repos), or "
        "clear github.secret_name / SBX_GITHUB_SECRET_NAME, then rerun `sbx deploy`",
        code="secret_missing",
    )


def _require_github_app_secret(cfg: BootstrapConfig, existing: set[str]) -> StepResult | None:
    """Fail-before-write check for the GitHub App Secret (SOR-177).

    Only applies when the resolved config identifies an App
    (``github_app.app_id`` / ``SBX_GITHUB_APP_ID``) AND named a Secret
    (``github_app.secret_name`` / ``SBX_GITHUB_APP_SECRET_NAME``) — the
    named Secret holds ``SBX_GITHUB_APP_PRIVATE_KEY`` so the remote
    control app can mint installation tokens. A missing named Secret is
    an explicit deploy failure rather than a silently dead authorize
    flow.
    """
    name = cfg.github_app_secret_name
    if not cfg.github_app_id or not name:
        return None
    if name in existing:
        return StepResult("secret:github-app", False, f"{name} present")
    raise BootstrapError(
        f"GitHub App Secret {name!r} is missing",
        hint=f"create it with `modal secret create {name} "
        'SBX_GITHUB_APP_PRIVATE_KEY="$(cat private-key.pem)"` (the App\'s '
        "generated private key), or clear github_app.secret_name / "
        "SBX_GITHUB_APP_SECRET_NAME, then rerun `sbx deploy`",
        code="secret_missing",
    )


def _require_account_secrets(cfg: BootstrapConfig, plane: Plane, existing: set[str]) -> StepResult:
    """Fail-before-write check for enabled providers' account Secrets.

    Every enabled-provider account record that names a Secret needs it at
    sandbox create. Deployment-managed names (``<account_secret_prefix><id>``)
    are satisfied by a stored ``credential/<id>`` blob — the materialize step
    below recreates them — while anything else must already exist. Accounts
    of providers that are not enabled carry no prerequisite.
    """
    if not plane.has_dict(cfg.accounts_dict):
        return StepResult("credentials:preflight", False, "no account registry yet")
    try:
        items = plane.dict_items(cfg.accounts_dict)
    except Exception as exc:
        raise BootstrapError(
            f"cannot read account credential store {cfg.accounts_dict!r}: {exc}",
            hint="check Modal Dict access, then rerun `sbx deploy`",
            code="account_credentials_unreadable",
        ) from exc

    blobs = {
        key[len("credential/") :]
        for key, _ in items
        if isinstance(key, str) and key.startswith("credential/")
    }
    missing: list[str] = []
    referenced = 0
    for key, value in items:
        if not (isinstance(key, str) and key.startswith("account/") and isinstance(value, dict)):
            continue
        if str(value.get("provider") or "") not in cfg.providers:
            continue  # provider not enabled — its credentials are not a prerequisite
        if str(value.get("status") or "active") == "disabled":
            continue  # never scheduled, so its Secret is never mounted
        name = str(value.get("secret_name") or "").strip()
        if not name:
            continue
        referenced += 1
        if name in existing:
            continue
        account_id = key[len("account/") :]
        if name == f"{cfg.account_secret_prefix}{account_id}" and account_id in blobs:
            continue  # materialized from the stored blob below
        missing.append(f"{name} (account {account_id})")
    if missing:
        preview = ", ".join(sorted(missing)[:5])
        if len(missing) > 5:
            preview += f", +{len(missing) - 5} more"
        raise BootstrapError(
            f"missing account credential Secret(s): {preview}",
            hint="import the credential with `python -m control.onboarding --modal import "
            "--provider <provider> --from <file>`, or create the Secret with "
            "`modal secret create`, or disable/remove the account — then rerun `sbx deploy`",
            code="account_secret_missing",
        )
    detail = (
        f"{referenced} referenced account Secret(s) satisfied"
        if referenced
        else "no account Secrets required"
    )
    return StepResult("credentials:preflight", False, detail)


def _resolve_cli_versions(
    config: BootstrapConfig,
    env: Mapping[str, str],
    *,
    versions_lock: str | None,
    fetch: Any = None,
    host_probe: Any = None,
) -> tuple[StepResult, Any, Path]:
    """Resolve + freeze provider CLI versions for this deployment (SOR-175).

    Runs once per deploy, before any image build: ``latest`` requests in
    ``runtime/packages.txt`` (or ``SBX_*_VERSION`` overrides) resolve
    upstream on the build host, pins pass through, and a ``--versions-lock``
    / ``SBX_VERSIONS_LOCK`` file replays a previous deployment's frozen set
    verbatim — the rollback lane. The outcome freezes to
    ``<state>/cli-versions.json`` as the deployment's version evidence and
    is passed to every image build so they all carry identical versions.
    """
    from runtime.versions import (
        VersionResolutionError,
        lock_out_path_for,
        resolve_versions,
        write_lock,
    )

    try:
        resolved = resolve_versions(
            env=env,
            # ``codex`` always resolves: its CLI rides in the base recipe of
            # every provider image, enabled or not.
            providers=set(config.providers) | {"codex"},
            fetch=fetch,
            host_probe=host_probe,
            lock=Path(versions_lock) if versions_lock else None,
        )
    except VersionResolutionError as exc:
        raise BootstrapError(
            f"cannot resolve provider CLI versions: {exc}",
            hint=exc.hint
            or "check runtime/packages.txt and SBX_*_VERSION "
            "overrides, or replay a frozen set via --versions-lock / "
            "SBX_VERSIONS_LOCK",
            code="version_resolution_failed",
        ) from exc
    lock_path = write_lock(
        resolved,
        lock_out_path_for(env)
        if env.get("SBX_VERSIONS_LOCK_OUT")
        else state_dir(env) / "cli-versions.json",
    )
    detail = (
        ", ".join(f"{p} {v}" for p, v in sorted(resolved.cli_versions().items()))
        or "nothing to resolve"
    )
    return (
        StepResult("versions", True, f"{detail} (frozen to {lock_path.name})"),
        resolved,
        lock_path,
    )


def _materialize_account_secrets(cfg: BootstrapConfig, plane: Plane) -> StepResult:
    """Materialize deployment-scoped account blobs as Modal Secrets.

    ``control.onboarding --modal import`` intentionally stores credential blobs
    in the durable accounts Dict.  Runtime sandboxes mount named Secrets, so
    deploy is the bridge: for accounts using this deployment's managed
    ``account_secret_prefix``, refresh the Secret from the stored blob.

    Accounts with an empty or custom Secret name are externally managed and
    are never overwritten here.  Credential values are never included in the
    report.
    """
    try:
        items = plane.dict_items(cfg.accounts_dict)
    except Exception as exc:
        raise BootstrapError(
            f"cannot read account credential store {cfg.accounts_dict!r}: {exc}",
            hint="check Modal Dict access, then rerun `sbx deploy`",
            code="account_credentials_unreadable",
        ) from exc

    records: dict[str, dict[str, Any]] = {}
    blobs: dict[str, dict[str, Any]] = {}
    for key, value in items:
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        if key.startswith("account/"):
            records[key[len("account/") :]] = value
        elif key.startswith("credential/"):
            blobs[key[len("credential/") :]] = value

    existing = plane.list_secret_names()
    materialized = 0
    refreshed = 0
    for account_id, blob in sorted(blobs.items()):
        if not is_valid_account_id(account_id):
            continue
        record = records.get(account_id)
        if record is None:
            continue
        provider = str(record.get("provider") or blob.get("provider") or "")
        if provider not in cfg.providers:
            continue  # provider not enabled — nothing to materialize for it
        secret_name = str(record.get("secret_name") or "").strip()
        expected = f"{cfg.account_secret_prefix}{account_id}"
        if secret_name != expected:
            # Empty/custom names are explicitly external-management lanes.
            continue
        if secret_name in existing:
            plane.delete_secret(secret_name)
            refreshed += 1
        payload = json.dumps(blob, ensure_ascii=False, separators=(",", ":"))
        plane.ensure_secret(secret_name, {"SBX_ACCOUNT_CREDENTIAL": payload})
        existing.add(secret_name)
        materialized += 1

    if materialized == 0:
        detail = "no deployment-managed account credentials to materialize"
    else:
        detail = f"{materialized} account credential Secret(s) ready"
        if refreshed:
            detail += f" ({refreshed} refreshed)"
    return StepResult("credentials:accounts", bool(materialized), detail)


def _probe_v1(
    base_url: str,
    token: str,
    *,
    transport: httpx.BaseTransport | None,
    attempts: int,
    sleep: Callable[[float], None],
) -> None:
    last: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            with V1Client(base_url, token, transport=transport, timeout=10.0) as client:
                client.me()
            return
        except (ApiError, httpx.HTTPError) as exc:
            last = exc
            sleep(1.0)
    raise BootstrapError(
        f"deployed app did not answer /v1/me at {base_url}: {last}",
        hint="run `sbx doctor` for a full check; cold start can take a minute",
        code="deploy_verify_failed",
    )


def deploy(
    cfg: ResolvedConfig,
    plane: Plane,
    *,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    probe_attempts: int = 5,
    version: str | None = None,
    versions_lock: str | None = None,
    fetch: Any = None,
    host_probe: Any = None,
) -> DeployReport:
    """Run the idempotent deploy pipeline; return a per-step report."""
    env = os.environ if env is None else env
    config = cfg.config
    steps: list[StepResult] = []

    require([check_python(), check_modal_package()])
    validate_providers(config.providers)
    workspace = _require_modal_auth(plane)
    steps.append(StepResult("modal-auth", False, f"workspace {workspace}"))

    # Preflight: everything that must exist before the first write, so a
    # missing prerequisite aborts with zero resources created. Credential
    # requirements derive from the configured providers only (SOR-116): the
    # shared Codex Secret is only required when codex is a selected provider
    # (``control.config.app_secret_names``) — unselected providers never
    # block onboarding (SOR-115) — and only enabled providers' referenced
    # account Secrets are checked.
    existing_secrets = plane.list_secret_names()
    if "codex" in config.providers:
        steps.append(_require_codex_secret(config, plane, env))
    steps.append(_require_account_secrets(config, plane, existing_secrets))
    github_step = _require_github_secret(config, existing_secrets)
    if github_step is not None:
        steps.append(github_step)
    github_app_step = _require_github_app_secret(config, existing_secrets)
    if github_app_step is not None:
        steps.append(github_app_step)

    # SOR-175: resolve + freeze provider CLI versions once, before any write
    # — a ``latest``/host-probe failure aborts with zero resources touched.
    versions_step, resolved_versions, versions_lock_path = _resolve_cli_versions(
        config, env, versions_lock=versions_lock, fetch=fetch, host_probe=host_probe
    )
    steps.append(versions_step)

    step, key_created, key_rotated = _ensure_bootstrap_secret(config, plane, env)
    steps.append(step)
    steps.append(_ensure_basic_secret(config, plane, env))

    created_dicts = [name for name in config.dict_names() if plane.ensure_dict(name)]
    steps.append(
        StepResult(
            "state",
            bool(created_dicts),
            "durable dicts ready"
            + (f" (created: {', '.join(created_dicts)})" if created_dicts else ""),
        )
    )
    steps.append(_materialize_account_secrets(config, plane))

    for provider in config.providers:
        plane.ensure_image(provider, config.image_name(provider), spec=resolved_versions.spec)
        steps.append(StepResult(f"image:{provider}", True, config.image_name(provider)))

    base_url = plane.deploy_app(config.modal_app_name, env=config.deploy_env())
    steps.append(StepResult("app", True, f"{config.modal_app_name} → {base_url}"))

    token, _ = load_or_create_key(key_path(env))
    _probe_v1(base_url, token, transport=transport, attempts=probe_attempts, sleep=sleep)
    steps.append(StepResult("verify", False, "/v1/me answered 200"))

    if cfg.sources.get("api_base_url") != "env" and config.api_base_url != base_url:
        save(_replace_base_url(config, base_url), cfg.path, env=env)
    version = version or app_version()
    _write_state(
        env,
        {
            "version": version,
            "deployed_at": _iso_now(),
            "app": config.modal_app_name,
            "app_url": base_url,
            "key_fingerprint": fingerprint(token),
            # SOR-175 version evidence: the frozen CLI set this deployment
            # built, plus where its lock file lives for replay/rollback.
            "cli_versions": resolved_versions.lock_payload(),
            "versions_lock": str(versions_lock_path),
        },
    )
    return DeployReport(
        steps=tuple(steps),
        base_url=base_url,
        version=version,
        key_created=key_created,
        key_rotated=key_rotated,
        cli_versions=resolved_versions.cli_versions(),
    )


def _replace_base_url(config: BootstrapConfig, base_url: str) -> BootstrapConfig:
    return replace(config, api_base_url=base_url)


def snapshot_durable(cfg: BootstrapConfig, plane: Plane) -> dict[str, int]:
    """Readable key counts of every durable store (upgrade invariant)."""
    snapshot: dict[str, int] = {}
    for name in cfg.dict_names():
        try:
            snapshot[name] = plane.dict_len(name)
        except Exception as exc:
            raise BootstrapError(
                f"durable store {name!r} is unreadable: {exc}",
                hint="upgrade aborted before touching anything; fix Modal access and retry",
                code="durable_unreadable",
            ) from exc
    return snapshot


@dataclass(frozen=True)
class UpgradeReport:
    deploy: DeployReport
    from_version: str
    to_version: str
    durable: dict[str, int]


def upgrade(
    cfg: ResolvedConfig,
    plane: Plane,
    *,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    probe_attempts: int = 5,
    version: str | None = None,
    versions_lock: str | None = None,
    fetch: Any = None,
    host_probe: Any = None,
) -> UpgradeReport:
    """Redeploy while proving durable stores stay readable end to end.

    Snapshots every durable Dict before and after the deploy; a store that
    stops answering aborts the upgrade as a failure, never silently.
    """
    env = os.environ if env is None else env
    prior = read_deploy_state(env)
    from_version = str(prior.get("version") or "unknown")
    before = snapshot_durable(cfg.config, plane)

    report = deploy(
        cfg,
        plane,
        env=env,
        transport=transport,
        sleep=sleep,
        probe_attempts=probe_attempts,
        version=version,
        versions_lock=versions_lock,
        fetch=fetch,
        host_probe=host_probe,
    )

    after = snapshot_durable(cfg.config, plane)
    lost = [name for name, count in before.items() if after.get(name, -1) < count]
    if lost:
        raise BootstrapError(
            f"durable stores lost keys across upgrade: {', '.join(lost)}",
            hint="inspect the Modal Dicts before retrying; do not rerun `sbx deploy` blindly",
            code="durable_lost",
        )
    return UpgradeReport(
        deploy=report,
        from_version=from_version,
        to_version=report.version,
        durable=after,
    )
