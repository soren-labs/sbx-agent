"""SOR-99: provider credential onboarding — safe add/verify/status/refresh/remove.

Turns an owner-prepared provider credential into a registered account without
ever exposing credential material: the registry keeps only metadata (frozen
``ports.Account``); the opaque blob ``{"provider": P, "files": {relpath:
content}}`` lives in the ``AccountStore`` credential slot (local file store
``0600`` or the ``modal.Dict sbx-accounts`` ``credential/<id>`` lane; deploy
materializes ``sbx-acct-<id>`` Secrets).

Flow (``python -m control.onboarding``)::

    providers            list supported provider descriptors
    add --provider P --from SRC   import a credential file/dir/blob (alias: import)
    verify ACCOUNT_ID    probe the stored credential (static / sandbox / auth seam)
    list / status        metadata only — never credential content
    refresh ACCOUNT_ID --from SRC   atomic write-back of a refreshed blob
    export ACCOUNT_ID --out PATH    write the stored blob to a 0600 file
    disable / enable     scheduling-safe status flips
    remove ACCOUNT_ID --yes         refuse while sessions are running

``--probe sandbox`` proves the blob restores and ``runner init`` accepts it —
it is NOT authoritative OAuth verification (the provider is never asked).
``--probe auth`` is the authoritative seam: after init it runs the provider
CLI's own auth check inside the same throwaway sandbox, so the provider's
answer decides ``ok`` vs ``auth_invalid``.

Import/refresh validation rejects unknown providers, provider/blob mismatch,
undeclared or escaping relpaths, symlinks, non-regular files, group/other
permissions, and schema-invalid content. A failed refresh never touches the
last good stored blob — all validation runs before the single atomic write.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import shlex
import stat
import sys
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from control.accounts import (
    PersistentAccountRegistry,
    is_valid_account_id,
    select_store,
    validate_account_id,
)
from control.config import account_secret_prefix
from control.ports import Account

# ------------------------------------------------------------------ descriptors


@dataclass(frozen=True)
class ProviderDescriptor:
    """How one provider's credential is onboarded.

    ``credential_files`` are the ``$HOME``-relative relpaths the blob may
    carry — the same set the sandbox runner restores and
    ``export-credentials`` writes back. ``content_kind`` selects the
    per-file schema check applied on import/refresh.

    ``support`` is the provider's Release 0.1 support tier exactly as
    published in ``docs/providers.md`` (``stable`` / ``experimental`` /
    ``preview`` / ``unsupported``) — user-facing output must never claim a
    tier above the real-account evidence. ``experimental`` is the import
    gate: providers whose production path is not part of the release
    require the explicit ``--experimental`` opt-in.
    """

    provider: str
    support: str  # release support tier — mirrors docs/providers.md
    experimental: bool  # import requires --experimental opt-in
    credential_files: tuple[str, ...]
    content_kind: str  # "json" | "toml"
    default_models: tuple[str, ...]
    summary: str


PROVIDER_DESCRIPTORS: tuple[ProviderDescriptor, ...] = (
    ProviderDescriptor(
        provider="codex",
        support="stable",
        experimental=False,
        credential_files=(".codex/auth.json",),
        content_kind="json",
        default_models=("gpt-5.6-luna",),
        summary="Codex CLI auth.json",
    ),
    ProviderDescriptor(
        provider="devin",
        support="experimental",
        experimental=False,
        credential_files=(".local/share/devin/credentials.toml",),
        content_kind="toml",
        default_models=("swe-2-high", "swe-2-medium"),
        summary="Devin/Windsurf credentials.toml",
    ),
    ProviderDescriptor(
        provider="antigravity",
        support="experimental",
        experimental=False,
        credential_files=(".gemini/antigravity-cli/antigravity-oauth-token",),
        content_kind="json",
        default_models=("gemini-3.8-flash-low",),
        summary="Antigravity OAuth token",
    ),
    ProviderDescriptor(
        provider="grok",
        support="experimental",
        experimental=False,
        credential_files=(".grok/auth.json",),
        content_kind="json",
        default_models=("grok-4.6",),
        summary="Grok auth.json",
    ),
    ProviderDescriptor(
        provider="opencode",
        support="experimental",
        experimental=False,
        credential_files=(".local/share/opencode/auth.json",),
        content_kind="json",
        default_models=(),
        summary="OpenCode auth.json",
    ),
    ProviderDescriptor(
        provider="claude",
        support="unsupported",
        experimental=True,
        credential_files=(".claude/.credentials.json",),
        content_kind="json",
        default_models=(),
        summary="Claude Code credentials (experimental — requires --experimental)",
    ),
)

_DESCRIPTOR_INDEX = {d.provider: d for d in PROVIDER_DESCRIPTORS}

# Env contract the sandbox runner consumes for this account (runner-cli.md).
CREDENTIAL_ENV = "SBX_ACCOUNT_CREDENTIAL"
ACCOUNT_ID_ENV = "SBX_ACCOUNT_ID"

# Probe outcomes surfaced by ``verify`` (and stored as ``last_error`` codes).
PROBE_STATUSES = (
    "ok",
    "auth_invalid",
    "provider_unavailable",
    "no_credential",
    "invalid_blob",
    "init_failed",
    "probe_unavailable",
)

# The smallest real request each provider CLI answers using only the
# restored credential — the fast probes the e2e gates run for agy/grok plus
# the CLIs' own auth-status commands. ``verify --probe auth`` execs them
# inside the throwaway sandbox; ``sbx`` discovery reuses the same argv on
# the host. Not every CLI exits non-zero on a dead login — grok/devin print
# a status page at rc 0, so ``classify_auth_output`` reads the output too.
PROVIDER_AUTH_CHECKS: dict[str, tuple[str, ...]] = {
    "codex": ("login", "status"),
    "devin": ("auth", "status"),
    "antigravity": ("models",),
    "grok": ("models",),
    "opencode": ("auth", "list"),
}

# SOR-204: argv tail that makes each provider CLI enumerate the account's
# servable models (and effort tiers where it emits them). ``capabilities``
# parses the output tolerantly; a CLI that lacks the subcommand exits
# non-zero and the catalog falls back to declared data.
# Real-CLI verified (cap-e2e acceptance): ``devin models`` alone is a usage
# error (the listing lives under ``models list``) and ``codex models`` is
# parsed as a prompt that spawns the TUI — the machine catalog is
# ``codex debug models``.
PROVIDER_MODEL_CHECKS: dict[str, tuple[str, ...]] = {
    "codex": ("debug", "models"),
    "devin": ("models", "list"),
    "antigravity": ("models",),
    "grok": ("models",),
    "opencode": ("models",),
}

# provider -> (*_BIN env override, default binary) — mirrors the runner
# adapters so tests can point the check at a fake CLI.
_PROVIDER_BINS: dict[str, tuple[str, str]] = {
    "codex": ("CODEX_BIN", "codex"),
    "devin": ("DEVIN_BIN", "devin"),
    "antigravity": ("AGY_BIN", "agy"),
    "grok": ("GROK_BIN", "grok"),
    "opencode": ("OPENCODE_BIN", "opencode"),
}

# Output markers that mean the credential itself is rejected, whatever the
# exit code says.
_AUTH_FAIL_MARKERS = (
    "not authenticated",
    "not logged in",
    "not signed in",
    "unauthorized",
    "authentication failed",
    "invalid api key",
    "no credentials",
    "please log in",
    "login required",
)

# CLIs that can exit 0 while reporting a logged-out status page — a passing
# rc alone is not proof; require the positive marker.
_MARKER_REQUIRED_PROVIDERS = ("devin", "grok")


def _provider_cli_argv(
    provider: str, tail: tuple[str, ...] | None, env: Mapping[str, str] | None
) -> list[str] | None:
    env = os.environ if env is None else env
    if tail is None:
        return None
    bin_env, default_bin = _PROVIDER_BINS.get(provider, ("", provider))
    tokens = shlex.split(env.get(bin_env) or default_bin)
    if not tokens:
        return None
    if len(tokens) == 1 and tokens[0].endswith(".py"):
        return [sys.executable, tokens[0], *tail]
    return [*tokens, *tail]


def provider_auth_argv(provider: str, env: Mapping[str, str] | None = None) -> list[str] | None:
    """Argv for the provider's own auth check; None when unsupported.

    ``*_BIN`` overrides mirror the runner adapters; a single ``.py`` token
    is re-executed with the current interpreter.
    """
    return _provider_cli_argv(provider, PROVIDER_AUTH_CHECKS.get(provider), env)


def provider_models_argv(provider: str, env: Mapping[str, str] | None = None) -> list[str] | None:
    """Argv for the provider's models listing (SOR-204); None when unsupported."""
    return _provider_cli_argv(provider, PROVIDER_MODEL_CHECKS.get(provider), env)


def output_has_auth_failure(output: str) -> bool:
    """True when CLI output carries a credential-rejection marker.

    Used by the models discovery probe: some CLIs (``grok models``) print an
    auth banner and still list a static catalog at rc 0, so exit status alone
    cannot distinguish a live account listing from a signed-out fallback.
    """
    low = output.lower()
    return any(marker in low for marker in _AUTH_FAIL_MARKERS)


def classify_auth_output(provider: str, returncode: int, output: str) -> str:
    """Map an auth check to ``ok`` / ``auth_invalid`` / ``probe_unavailable``."""
    low = output.lower()
    if any(marker in low for marker in _AUTH_FAIL_MARKERS):
        return "auth_invalid"
    if provider in _MARKER_REQUIRED_PROVIDERS:
        if returncode == 0 and "logged in" in low:
            return "ok"
        return "auth_invalid" if returncode != 0 else "probe_unavailable"
    if provider == "opencode":
        # ``auth list`` prints the CLI's credential registry; empty output
        # means it never registered the file (inconclusive, not rejected).
        if returncode == 0 and output.strip():
            return "ok"
        return "auth_invalid" if returncode != 0 else "probe_unavailable"
    return "ok" if returncode == 0 else "auth_invalid"


class OnboardingError(Exception):
    """Structured onboarding refusal. ``code`` is a machine-readable tag."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def descriptor_for(provider: str) -> ProviderDescriptor:
    try:
        return _DESCRIPTOR_INDEX[provider]
    except KeyError:
        raise OnboardingError(
            "unknown_provider",
            f"unknown provider {provider!r}; supported: {sorted(_DESCRIPTOR_INDEX)}",
        ) from None


def _check_account_id(account_id: Any) -> str:
    """Reject ids that could escape the account store's file layout.

    Delegates to the shared ``control.accounts.validate_account_id`` seam
    (SOR-105): ``FileAccountStore`` maps an id to ``accounts/<id>.json`` /
    ``credentials/<id>.json``, and the Modal lane embeds it verbatim in Dict
    keys and the ``sbx-acct-<id>`` Secret name — only unreserved filename
    characters are safe.
    """
    try:
        return validate_account_id(account_id)
    except ValueError as exc:
        raise OnboardingError("invalid_account_id", str(exc)) from exc


# ------------------------------------------------------------------ validation


def _check_relpath(relpath: Any) -> None:
    """Same rules as ``runtime.runner.credentials`` blob restore."""
    if not isinstance(relpath, str) or not relpath.strip():
        raise OnboardingError("invalid_blob", f"invalid credential path: {relpath!r}")
    if "\x00" in relpath or "\\" in relpath:
        raise OnboardingError("unsafe_path", f"invalid credential path: {relpath!r}")
    parts = PurePosixPath(relpath).parts
    if PurePosixPath(relpath).is_absolute() or not parts or any(p in ("..", ".") for p in parts):
        raise OnboardingError("unsafe_path", f"credential path escapes $HOME: {relpath!r}")


def _decode_content(value: Any) -> bytes:
    """Contract blob entry -> bytes (str, ``content``, or ``content_b64``)."""
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, dict):
        if "content_b64" in value:
            try:
                return base64.b64decode(str(value["content_b64"]), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise OnboardingError(
                    "invalid_blob", "credential file content_b64 is not valid base64"
                ) from exc
        if "content" in value:
            return str(value["content"]).encode("utf-8")
    raise OnboardingError(
        "invalid_blob", "credential file entries must be a string or {content|content_b64}"
    )


def _check_content_schema(desc: ProviderDescriptor, relpath: str, content: bytes) -> None:
    if not content:
        raise OnboardingError("schema_mismatch", f"{relpath}: credential file is empty")
    if desc.content_kind == "json":
        try:
            parsed = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OnboardingError(
                "schema_mismatch", f"{relpath}: expected a JSON object ({desc.summary})"
            ) from exc
        if not isinstance(parsed, dict):
            raise OnboardingError(
                "schema_mismatch", f"{relpath}: expected a JSON object ({desc.summary})"
            )
        return
    if desc.content_kind == "toml":
        try:
            parsed = tomllib.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise OnboardingError(
                "schema_mismatch", f"{relpath}: expected TOML ({desc.summary})"
            ) from exc
        if not isinstance(parsed, dict) or not parsed:
            raise OnboardingError(
                "schema_mismatch", f"{relpath}: expected a non-empty TOML table ({desc.summary})"
            )
        return
    raise OnboardingError("unknown_provider", f"unsupported content kind {desc.content_kind!r}")


def validate_credential_blob(
    provider: str, blob: Any, *, require_full: bool = True
) -> dict[str, Any]:
    """Validate a ``{"provider": P, "files": {...}}`` blob for ``provider``.

    Checks provider consistency, relpath safety, that every path is one the
    descriptor declares, and the per-provider content schema. ``require_full``
    demands the complete declared file set (import/refresh both write a whole
    credential set — a partial write would leave a mixed-generation blob).
    """
    desc = descriptor_for(provider)
    if not isinstance(blob, dict):
        raise OnboardingError("invalid_blob", "credential blob must be a JSON object")
    blob_provider = blob.get("provider")
    if blob_provider != provider:
        raise OnboardingError(
            "provider_mismatch",
            f"credential blob provider {blob_provider!r} does not match {provider!r}",
        )
    files = blob.get("files")
    if not isinstance(files, dict) or not files:
        raise OnboardingError("invalid_blob", "credential blob needs a non-empty 'files' object")
    declared = set(desc.credential_files)
    for relpath, value in files.items():
        _check_relpath(relpath)
        if relpath not in declared:
            raise OnboardingError(
                "unsafe_path",
                f"undeclared credential path {relpath!r} for provider {provider!r}",
            )
        _check_content_schema(desc, str(relpath), _decode_content(value))
    missing = declared - set(files)
    if require_full and missing:
        raise OnboardingError(
            "missing_credential_file",
            f"credential blob missing file(s): {', '.join(sorted(missing))}",
        )
    return blob


def _looks_like_blob(data: Any) -> bool:
    return isinstance(data, dict) and "provider" in data and "files" in data


def _read_source_file(path: Path, *, root: Path, allow_open_permissions: bool) -> bytes:
    """Read one credential file after path/symlink/type/mode checks."""
    if path.is_symlink():
        raise OnboardingError("unsafe_path", f"credential source is a symlink: {path}")
    resolved_root = root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise OnboardingError("unsafe_path", f"credential source escapes import root: {path}")
    try:
        mode = resolved.lstat().st_mode
    except OSError as exc:
        raise OnboardingError("invalid_source", f"cannot stat {path}: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise OnboardingError("unsafe_path", f"credential source is not a regular file: {path}")
    if not allow_open_permissions and stat.S_IMODE(mode) & 0o077:
        raise OnboardingError(
            "bad_permissions",
            f"{path}: credential file is group/other accessible "
            f"({oct(stat.S_IMODE(mode))}); chmod 600 or pass --allow-open-permissions",
        )
    try:
        return resolved.read_bytes()
    except OSError as exc:
        raise OnboardingError("invalid_source", f"cannot read {path}: {exc}") from exc


def _store_entry(content: bytes) -> str | dict[str, str]:
    """Blob ``files`` value: text stays a str, binary degrades to content_b64."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return {"content_b64": base64.b64encode(content).decode("ascii")}


def collect_credential_blob(
    provider: str,
    source: Path | str,
    *,
    allow_open_permissions: bool = False,
    stdin_text: str | None = None,
) -> dict[str, Any]:
    """Build a validated credential blob from ``source``.

    ``source`` may be a credential file, a directory holding the declared
    files (``source/<relpath>`` or ``source/<basename>``), a blob JSON file,
    or ``"-"`` (blob JSON on stdin — e.g. piped ``runner export-credentials``
    output). Everything is validated before anything is written.
    """
    desc = descriptor_for(provider)

    if str(source) == "-":
        text = stdin_text if stdin_text is not None else sys.stdin.read()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OnboardingError("invalid_blob", "stdin is not a credential blob JSON") from exc
        return validate_credential_blob(provider, data)

    path = Path(source).expanduser()
    if path.is_symlink():
        raise OnboardingError("unsafe_path", f"credential source is a symlink: {path}")
    if path.is_dir():
        root = path.resolve()
        files: dict[str, str | dict[str, str]] = {}
        missing: list[str] = []
        for rel in desc.credential_files:
            content: bytes | None = None
            for candidate in (path / rel, path / PurePosixPath(rel).name):
                if candidate.exists() or candidate.is_symlink():
                    content = _read_source_file(
                        candidate, root=root, allow_open_permissions=allow_open_permissions
                    )
                    break
            if content is None:
                missing.append(rel)
            else:
                files[rel] = _store_entry(content)
        if missing:
            raise OnboardingError(
                "missing_credential_file",
                f"--from {path}: missing credential file(s): {', '.join(missing)}",
            )
        return validate_credential_blob(provider, {"provider": provider, "files": files})

    if not path.is_file():
        raise OnboardingError("invalid_source", f"--from {path}: not a file or directory")

    raw = _read_source_file(path, root=path.parent, allow_open_permissions=allow_open_permissions)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if _looks_like_blob(data):
        return validate_credential_blob(provider, data)
    if len(desc.credential_files) != 1:
        raise OnboardingError(
            "invalid_source",
            f"--from {path}: provider {provider!r} needs {len(desc.credential_files)} "
            "credential files; pass the containing directory or a blob JSON",
        )
    rel = desc.credential_files[0]
    return validate_credential_blob(
        provider, {"provider": provider, "files": {rel: _store_entry(raw)}}
    )


# ------------------------------------------------------------------ verify seam


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a credential probe. ``status`` is one of PROBE_STATUSES."""

    status: str
    detail: str = ""


@runtime_checkable
class CredentialProbe(Protocol):
    """Verify seam: check a stored credential without exposing it."""

    def probe(self, account: Account, blob: dict[str, Any] | None) -> ProbeResult:
        """Return a structured ProbeResult; never raises credential material."""


class StaticCredentialProbe:
    """Structural probe: blob presence + schema validation, no sandbox.

    Author-phase default. Real per-provider CLI probes (a minimal sandbox
    running ``runner init`` then destroyed) plug in behind the same seam via
    ``SandboxVerifyProbe`` or any ``CredentialProbe`` implementation.
    """

    def probe(self, account: Account, blob: dict[str, Any] | None) -> ProbeResult:
        if blob is None and not account.secret_name:
            return ProbeResult("no_credential", "no stored credential blob and no secret_name")
        if blob is None:
            # Secret-only account: the named Modal Secret is materialized at
            # deploy time; there is nothing local to validate statically.
            return ProbeResult("probe_unavailable", "credential lives in a named Secret")
        try:
            validate_credential_blob(account.provider, blob)
        except OnboardingError as exc:
            return ProbeResult("invalid_blob", exc.code)
        return ProbeResult("ok")


class SandboxVerifyProbe:
    """Minimal-sandbox probe: ``runner init`` with the account credential.

    Mirrors the ``/v1/accounts/{id}/verify`` route: create a throwaway
    sandbox, run ``runner init`` (restores the blob; fails on provider
    mismatch / malformed credential / missing adapter), terminate. The
    provider CLI itself is never invoked, keeping the probe cheap — the
    result proves restore + runner setup only, NOT that the credential
    still authenticates. The authoritative provider check slots behind the
    ``_after_init`` hook — see :class:`SandboxAuthVerifyProbe`.
    """

    def __init__(
        self,
        backend: Any,
        runner_cmd: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        model: str | None = None,
    ) -> None:
        self._backend = backend
        self._runner_cmd = list(runner_cmd)
        self._env = dict(env or {})
        self._model = model

    def _after_init(self, handle: Any, account: Account, env: Mapping[str, str]) -> ProbeResult:
        """Hook once ``runner init`` succeeded, before sandbox teardown.

        ``env`` is the init exec env — it may carry the credential blob, so
        subclasses must strip it before exec'ing the provider CLI.
        """
        return ProbeResult("ok")

    def probe(self, account: Account, blob: dict[str, Any] | None) -> ProbeResult:
        from control.backend import SandboxSpec
        from control.sandbox_io import sandbox_env

        if blob is None and not account.secret_name:
            return ProbeResult("no_credential", "no stored credential blob and no secret_name")
        handle = None
        try:
            handle = self._backend.create(
                SandboxSpec(
                    tags={
                        "purpose": "account-verify",
                        "provider": account.provider,
                        "account_id": account.id,
                    },
                    secrets=[account.secret_name] if account.secret_name else [],
                    env=self._env,
                )
            )
            extra = {ACCOUNT_ID_ENV: account.id}
            if blob:
                extra[CREDENTIAL_ENV] = json.dumps(blob, ensure_ascii=False)
            env = sandbox_env(handle, extra)
            model = self._model or (account.models[0] if account.models else "gpt-5.6-luna")
            argv = [
                *self._runner_cmd,
                "init",
                "--auth",
                "auth_json",
                "--model",
                model,
                "--provider",
                account.provider,
                "--account-id",
                account.id,
            ]
            proc = self._backend.exec(handle, argv, env=env)
            for _ in proc.stdout:
                pass
            code = proc.wait()
            if code == 0:
                return self._after_init(handle, account, env)
            if code == 5:
                return ProbeResult("auth_invalid")
            return ProbeResult("init_failed", f"runner init exited {code}")
        except NotImplementedError:
            return ProbeResult("probe_unavailable", "sandbox backend not implemented")
        except Exception:
            return ProbeResult("provider_unavailable", "probe_exec_failed")
        finally:
            if handle is not None:
                try:
                    self._backend.terminate(handle)
                except Exception:
                    pass


class SandboxAuthVerifyProbe(SandboxVerifyProbe):
    """Authoritative probe: ``runner init`` + the provider's own auth check.

    After the credential restores cleanly, execs the provider CLI's auth
    command (``PROVIDER_AUTH_CHECKS`` — e.g. ``codex login status``,
    ``agy models``) inside the same throwaway sandbox with ``HOME`` pointed
    at the restored home. The provider's answer — not the restore — decides
    ``ok`` vs ``auth_invalid``.
    """

    def __init__(
        self,
        backend: Any,
        runner_cmd: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        model: str | None = None,
        bin_env: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(backend, runner_cmd, env=env, model=model)
        self._bin_env = bin_env

    def _after_init(self, handle: Any, account: Account, env: Mapping[str, str]) -> ProbeResult:
        argv = provider_auth_argv(account.provider, env=self._bin_env)
        if argv is None:
            return ProbeResult(
                "probe_unavailable", f"no auth check for provider {account.provider!r}"
            )
        check_env = provider_cli_env(account.provider, env, handle.root / "home")
        try:
            proc = self._backend.exec(handle, argv, env=check_env)
            output = "\n".join(proc.stdout)
            code = proc.wait()
        except Exception:
            return ProbeResult("probe_unavailable", "auth_check_exec_failed")
        return ProbeResult(classify_auth_output(account.provider, code, output))


# ------------------------------------------------------------------ service


def provider_cli_env(provider: str, env: Mapping[str, str], home: Path) -> dict[str, str]:
    """Exec env for a provider CLI inside a probe sandbox.

    Credential-bearing control vars are stripped — the restored files under
    the sandbox ``HOME`` are the only credential source. ``devin`` and
    ``opencode`` additionally pin XDG dirs (same pinning as
    ``runtime.image`` devin_runtime_env) so the lookup stays at the
    restored home.
    """
    check_env = {
        k: v
        for k, v in env.items()
        if k
        not in (
            CREDENTIAL_ENV,
            ACCOUNT_ID_ENV,
            "CODEX_AUTH_JSON",
            "SBX_PROVIDER_API_KEY",
            "SBX_PROVIDER_BASE_URL",
        )
    }
    check_env["HOME"] = str(home)
    check_env.setdefault("PATH", os.environ.get("PATH", os.defpath))
    if provider in ("devin", "opencode"):
        check_env.update(
            {
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_STATE_HOME": str(home / ".local" / "state"),
            }
        )
    return check_env


class CredentialSecretWriter(Protocol):
    """Recreate a named Modal Secret in place. Never logs ``env`` values."""

    def refresh(self, secret_name: str, env: dict[str, str]) -> None: ...


class OnboardingService:
    """Provider credential lifecycle over a ``PersistentAccountRegistry``.

    Every method returns metadata or raises ``OnboardingError`` — credential
    content is never returned, logged, or printed.

    ``secret_writer`` (optional) materializes the deployment-managed
    ``<account_secret_prefix><id>`` Modal Secret on credential writes so a
    relinked credential reaches sandboxes without a full redeploy — the same
    bridge ``sbx deploy`` performs and ``CredentialSync.writeback`` applies
    on rotation commits.
    """

    def __init__(
        self,
        registry: PersistentAccountRegistry,
        probe: CredentialProbe | None = None,
        secret_writer: CredentialSecretWriter | None = None,
    ) -> None:
        self._registry = registry
        self._probe = probe or StaticCredentialProbe()
        self._secret_writer = secret_writer

    def _materialize_secret(self, account: Account, blob: dict[str, Any]) -> str:
        """Recreate the deployment-managed Secret for ``account``.

        Only the ``<prefix><id>`` naming convention is managed here — empty
        or custom ``secret_name`` values are externally managed lanes and are
        never overwritten. Returns ``skipped`` | ``refreshed`` | ``failed``;
        a Secret failure never undoes the store commit, which stays
        authoritative.
        """
        if self._secret_writer is None:
            return "skipped"
        expected = f"{account_secret_prefix()}{account.id}"
        if account.secret_name != expected:
            return "skipped"
        try:
            self._secret_writer.refresh(
                account.secret_name,
                {CREDENTIAL_ENV: json.dumps(blob, ensure_ascii=False, separators=(",", ":"))},
            )
        except Exception:
            return "failed"
        return "refreshed"

    @property
    def registry(self) -> PersistentAccountRegistry:
        return self._registry

    def _get_account(self, account_id: str) -> Account:
        """Fetch ``account_id`` or raise; validates the id first so a
        caller-supplied value can never reach a store path."""
        _check_account_id(account_id)
        account = self._registry.get(account_id)
        if account is None:
            raise OnboardingError("account_not_found", f"account {account_id!r} not found")
        return account

    # -- write paths

    def add(
        self,
        provider: str,
        source: Path | str,
        *,
        label: str = "",
        account_id: str | None = None,
        slots: int = 1,
        models: Sequence[str] | None = None,
        experimental_ok: bool = False,
        allow_open_permissions: bool = False,
        stdin_text: str | None = None,
    ) -> Account:
        """Import a credential as a new account. Fails before any write."""
        desc = descriptor_for(provider)
        if desc.experimental and not experimental_ok:
            raise OnboardingError(
                "experimental_provider",
                f"provider {provider!r} is experimental; pass --experimental to proceed",
            )
        if slots < 1:
            raise OnboardingError("invalid_source", "--slots must be >= 1")
        blob = collect_credential_blob(
            provider,
            source,
            allow_open_permissions=allow_open_permissions,
            stdin_text=stdin_text,
        )
        account_id = _check_account_id(account_id or f"acct-{provider}-{uuid.uuid4().hex[:8]}")
        if self._registry.get(account_id) is not None:
            raise OnboardingError("account_exists", f"account {account_id!r} already exists")
        account = Account(
            id=account_id,
            provider=provider,
            label=label or account_id,
            status="active",
            max_concurrent=slots,
            secret_name=f"{account_secret_prefix()}{account_id}",
            # Declared-seed models only — SOR-204's capability discovery
            # supersedes them once the account's first probe lands.
            models=(
                tuple(models)
                if models
                else (
                    tuple(
                        part.strip()
                        for part in os.environ.get(f"SBX_{provider.upper()}_MODELS", "").split(",")
                        if part.strip()
                    )
                    or desc.default_models
                )
            ),
            created_at=datetime.now(UTC).isoformat(),
        )
        self._registry.put(account)
        try:
            self._registry.put_credential_blob(account_id, blob)
        except Exception:
            # Never leave an active, credential-less account the scheduler can
            # pick: roll the record back if the blob write fails.
            self._registry.remove(account_id)
            raise
        try:
            from control.credlifecycle import CredentialLifecycleService

            CredentialLifecycleService(self._registry).note_credential(account_id, blob)
        except Exception:
            pass
        return account

    def refresh(
        self,
        account_id: str,
        source: Path | str,
        *,
        allow_open_permissions: bool = False,
        stdin_text: str | None = None,
    ) -> dict[str, Any]:
        """Atomically replace the stored credential; last-good is preserved.

        All validation completes before the single store write, so a refused
        refresh leaves the previous blob byte-identical. Returns
        ``{"changed": bool, "files": int, "secret": str}`` — never blob
        content. With a ``secret_writer`` configured (the ``--modal`` CLI
        path), the deployment-managed Secret is recreated in place so the
        new credential reaches sandboxes without a redeploy.
        """
        account = self._get_account(account_id)
        blob = collect_credential_blob(
            account.provider,
            source,
            allow_open_permissions=allow_open_permissions,
            stdin_text=stdin_text,
        )
        old = self._registry.get_credential_blob(account_id)
        self._registry.put_credential_blob(account_id, blob)
        try:
            from control.credlifecycle import CredentialLifecycleService

            CredentialLifecycleService(self._registry).note_credential(account_id, blob)
        except Exception:
            pass
        secret = self._materialize_secret(account, blob)
        return {"changed": blob != old, "files": len(blob["files"]), "secret": secret}

    def export(self, account_id: str, out_path: Path | str) -> Path:
        """Write the stored blob to ``out_path`` (mode 600, atomic)."""
        self._get_account(account_id)
        blob = self._registry.get_credential_blob(account_id)
        if blob is None:
            raise OnboardingError(
                "no_credential", f"account {account_id!r} has no stored credential blob"
            )
        path = Path(out_path).expanduser()
        if path.is_symlink():
            raise OnboardingError("unsafe_path", f"export target is a symlink: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.unlink(missing_ok=True)  # a stale tmp could carry a permissive mode
        data = json.dumps(blob, ensure_ascii=False) + "\n"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        tmp.replace(path)
        return path

    # -- verify

    def verify(self, account_id: str) -> tuple[ProbeResult, Account]:
        """Probe the stored credential and fold the result into the record.

        ``auth_invalid`` / credential-shape failures mark the account
        ``invalid`` — the scheduler only auto-picks ``active`` accounts, so a
        bad credential is immediately excluded from auto scheduling.
        ``provider_unavailable`` / ``probe_unavailable`` keep the current
        status and only record ``last_error``. A successful probe reactivates
        ``invalid``/``cooling`` accounts but never re-enables a ``disabled``
        one (operator intent wins).
        """
        account = self._get_account(account_id)
        blob = self._registry.get_credential_blob(account_id)
        result = self._probe.probe(account, blob)
        status = result.status
        try:
            from control.credlifecycle import CredentialLifecycleService

            lifecycle = CredentialLifecycleService(self._registry)
            if status == "ok" and blob is not None:
                lifecycle.note_credential(account_id, blob)
            elif status == "auth_invalid":
                lifecycle.on_auth_invalid(account_id, detail=status, mark_account=False)
        except Exception:
            pass
        if status == "ok":
            new_status = "disabled" if account.status == "disabled" else "active"
            updated = self._registry.mark_status(account_id, new_status, last_error=None)
        elif status in ("auth_invalid", "no_credential", "invalid_blob", "init_failed"):
            updated = self._registry.mark_status(account_id, "invalid", last_error=status)
        else:  # provider_unavailable / probe_unavailable: record, don't demote
            updated = self._registry.mark_status(
                account_id,
                account.status,
                cooldown_until=account.cooldown_until,
                last_error=status,
            )
        return result, updated

    # -- status / lifecycle

    def describe(self, account: Account) -> dict[str, Any]:
        """Metadata-only account descriptor (SOR-99 §1 shape)."""
        desc = _DESCRIPTOR_INDEX.get(account.provider)
        # A record stored with a non-conformant id (planted/corrupt) stays
        # visible but must never reach the running/blob lanes — per-id store
        # ops refuse it fail-closed (SOR-105).
        id_ok = is_valid_account_id(account.id)
        return {
            "account_id": account.id,
            "provider": account.provider,
            "label": account.label,
            "status": account.status,
            "support": desc.support if desc else "unknown",
            "credential_files": list(desc.credential_files) if desc else [],
            "credential_env": CREDENTIAL_ENV,
            "secret_name": account.secret_name,
            "models": list(account.models),
            "max_concurrent": account.max_concurrent,
            "running": self._registry.running_count(account.id) if id_ok else 0,
            "has_credential": (
                id_ok and self._registry.get_credential_blob(account.id) is not None
            ),
            "created_at": account.created_at,
            "last_used_at": account.last_used_at,
            "cooldown_until": account.cooldown_until,
            "last_error": account.last_error,
        }

    def status(self, account_id: str) -> dict[str, Any]:
        return self.describe(self._get_account(account_id))

    def list(self, provider: str | None = None) -> list[dict[str, Any]]:
        return [self.describe(a) for a in self._registry.list(provider)]

    def _set_status(self, account_id: str, status: str) -> Account:
        _check_account_id(account_id)
        try:
            return self._registry.mark_status(account_id, status)
        except KeyError:
            raise OnboardingError(
                "account_not_found", f"account {account_id!r} not found"
            ) from None

    def disable(self, account_id: str) -> Account:
        return self._set_status(account_id, "disabled")

    def enable(self, account_id: str) -> Account:
        return self._set_status(account_id, "active")

    def remove(self, account_id: str, *, confirm: bool = False) -> None:
        """Delete record + blob. Requires ``--yes``; refuses running accounts."""
        self._get_account(account_id)
        if not confirm:
            raise OnboardingError(
                "confirmation_required", f"removing {account_id!r} is irreversible; pass --yes"
            )
        running = self._registry.running_count(account_id)
        if running > 0:
            raise OnboardingError(
                "account_in_use",
                f"account {account_id!r} has {running} running session(s); "
                "disable it and wait for them to finish first",
            )
        self._registry.remove(account_id)


# --------------------------------------------------------------------- CLI


def _print_account_row(entry: dict[str, Any]) -> None:
    print(
        f"{entry['account_id']}\t{entry['provider']}\t{entry['status']}\t"
        f"running={entry['running']}/{entry['max_concurrent']}\t"
        f"credential={'yes' if entry['has_credential'] else 'no'}\t"
        f"last_error={entry['last_error'] or '-'}\t{entry['label']}"
    )


def _default_probe(probe_kind: str, runner_cmd: str | None) -> CredentialProbe:
    if probe_kind in ("sandbox", "auth"):
        from control.backend import LocalProcessBackend

        cmd = shlex.split(runner_cmd) if runner_cmd else [sys.executable, "-m", "runtime.runner"]
        repo_root = str(Path(__file__).resolve().parent.parent)
        cls = SandboxAuthVerifyProbe if probe_kind == "auth" else SandboxVerifyProbe
        return cls(LocalProcessBackend(), cmd, env={"PYTHONPATH": repo_root})
    return StaticCredentialProbe()


def main(argv: list[str] | None = None) -> int:
    """``python -m control.onboarding`` — provider credential administration."""
    parser = argparse.ArgumentParser(prog="control.onboarding")
    parser.add_argument("--store-dir", default=None, help="file store root (local mode)")
    parser.add_argument(
        "--modal", action="store_true", help="use the modal.Dict store (SBX_BACKEND=modal)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_providers = sub.add_parser("providers", help="list provider descriptors")
    p_providers.add_argument("--json", action="store_true")

    p_add = sub.add_parser("add", aliases=["import"], help="import a credential as a new account")
    p_add.add_argument("--provider", required=True)
    p_add.add_argument("--label", default="")
    p_add.add_argument(
        "--from", dest="source", required=True, help="credential file/dir/blob, or - for stdin"
    )
    p_add.add_argument("--account-id", default=None)
    p_add.add_argument("--slots", type=int, default=1, help="max_concurrent")
    p_add.add_argument("--models", default=None, help="comma-separated advertised models")
    p_add.add_argument("--experimental", action="store_true", help="allow experimental providers")
    p_add.add_argument(
        "--allow-open-permissions",
        action="store_true",
        help="accept credential files readable by group/other",
    )

    p_verify = sub.add_parser("verify", help="probe the stored credential")
    p_verify.add_argument("account_id")
    p_verify.add_argument(
        "--probe",
        choices=["static", "sandbox", "auth"],
        default="static",
        help="static = schema check; sandbox = runner-init restore check "
        "(not authoritative auth); auth = init + the provider CLI's own "
        "auth check (authoritative)",
    )
    p_verify.add_argument("--runner-cmd", default=None, help="runner argv prefix (sandbox probe)")
    p_verify.add_argument("--json", action="store_true")

    p_list = sub.add_parser("list", help="list accounts (metadata only)")
    p_list.add_argument("--provider", default=None)
    p_list.add_argument("--json", action="store_true")

    p_status = sub.add_parser("status", help="one account descriptor (metadata only)")
    p_status.add_argument("account_id")
    p_status.add_argument("--json", action="store_true")

    p_refresh = sub.add_parser(
        "refresh", help="atomically replace the stored credential (export-credentials write-back)"
    )
    p_refresh.add_argument("account_id")
    p_refresh.add_argument(
        "--from", dest="source", required=True, help="credential file/dir/blob, or - for stdin"
    )
    p_refresh.add_argument("--allow-open-permissions", action="store_true")

    p_export = sub.add_parser("export", help="write the stored blob to a 0600 file")
    p_export.add_argument("account_id")
    p_export.add_argument("--out", required=True, help="output path (never stdout)")

    for name in ("disable", "enable"):
        p = sub.add_parser(name)
        p.add_argument("account_id")

    p_remove = sub.add_parser("remove", help="delete account + credential (requires --yes)")
    p_remove.add_argument("account_id")
    p_remove.add_argument("--yes", action="store_true", help="confirm irreversible removal")

    args = parser.parse_args(argv)
    backend = "modal" if args.modal else os.environ.get("SBX_BACKEND", "local")
    registry = PersistentAccountRegistry(select_store(store_dir=args.store_dir, backend=backend))
    secret_writer = None
    if backend == "modal":
        from control.credsync import ModalCredentialSecretWriter

        secret_writer = ModalCredentialSecretWriter()
    service = OnboardingService(registry, secret_writer=secret_writer)

    try:
        if args.cmd == "providers":
            entries = [
                {
                    "provider": d.provider,
                    "support": d.support,
                    "credential_files": list(d.credential_files),
                    "credential_env": CREDENTIAL_ENV,
                    "default_models": list(d.default_models),
                    "summary": d.summary,
                }
                for d in PROVIDER_DESCRIPTORS
            ]
            if args.json:
                print(json.dumps({"providers": entries}, indent=2))
            else:
                for e in entries:
                    print(
                        f"{e['provider']}\t{e['support']}\t{','.join(e['credential_files'])}\t{e['summary']}"
                    )
            return 0

        if args.cmd in ("add", "import"):
            models = (
                tuple(m.strip() for m in args.models.split(",") if m.strip())
                if args.models
                else None
            )
            account = service.add(
                args.provider,
                args.source,
                label=args.label,
                account_id=args.account_id,
                slots=args.slots,
                models=models,
                experimental_ok=args.experimental,
                allow_open_permissions=args.allow_open_permissions,
            )
            blob = registry.get_credential_blob(account.id) or {}
            print(
                f"added {account.id} provider={account.provider} "
                f"files={len(blob.get('files') or {})} status={account.status}"
            )
            return 0

        if args.cmd == "verify":
            service = OnboardingService(registry, _default_probe(args.probe, args.runner_cmd))
            result, account = service.verify(args.account_id)
            entry = service.describe(account)
            entry["probe"] = {"status": result.status, "detail": result.detail}
            if args.json:
                print(json.dumps(entry, indent=2))
            else:
                print(
                    f"{account.id}\tprobe={result.status}"
                    + (f" ({result.detail})" if result.detail else "")
                    + f"\tstatus={account.status}\tlast_error={account.last_error or '-'}"
                )
            return 0 if result.status == "ok" else 1

        if args.cmd == "list":
            entries = service.list(args.provider)
            if args.json:
                print(json.dumps({"accounts": entries}, indent=2))
            else:
                for entry in entries:
                    _print_account_row(entry)
            return 0

        if args.cmd == "status":
            entry = service.status(args.account_id)
            if args.json:
                print(json.dumps(entry, indent=2))
            else:
                _print_account_row(entry)
                print(f"  secret_name={entry['secret_name']}")
                print(f"  models={','.join(entry['models']) or '-'}")
                print(f"  credential_files={','.join(entry['credential_files'])}")
                print(f"  created_at={entry['created_at'] or '-'}")
                print(f"  last_used_at={entry['last_used_at'] or '-'}")
                print(f"  cooldown_until={entry['cooldown_until'] or '-'}")
            return 0

        if args.cmd == "refresh":
            outcome = service.refresh(
                args.account_id,
                args.source,
                allow_open_permissions=args.allow_open_permissions,
            )
            state = "replaced" if outcome["changed"] else "unchanged"
            print(
                f"refreshed {args.account_id}: {state} files={outcome['files']} "
                f"secret={outcome['secret']}"
            )
            return 0 if outcome["secret"] != "failed" else 1

        if args.cmd == "export":
            path = service.export(args.account_id, args.out)
            print(f"exported {args.account_id} -> {path} (mode 600)")
            return 0

        if args.cmd == "disable":
            service.disable(args.account_id)
            print(f"disabled {args.account_id}")
            return 0
        if args.cmd == "enable":
            service.enable(args.account_id)
            print(f"enabled {args.account_id}")
            return 0
        if args.cmd == "remove":
            service.remove(args.account_id, confirm=args.yes)
            print(f"removed {args.account_id}")
            return 0
        return 2
    except OnboardingError as exc:
        print(f"error[{exc.code}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
