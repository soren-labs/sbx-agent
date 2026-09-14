"""``SBX_ACCOUNT_CREDENTIAL`` blob restore + agent child env hygiene (SOR-74).

Contract: ``docs/contracts/runner-cli.md`` §凭证注入 — the control plane
injects ``{"provider": P, "files": {relpath: content}}`` via env; ``runner
init`` restores each file under ``$HOME`` (``$SBX_WORK/home``) with mode 600,
and the provider CLI child must never inherit the blob or host/Desktop auth
bridges.

Devin specifics: ``ACP_BACKEND`` makes the CLI treat an ACP host (Devin
Desktop / Windsurf) as the sole credential source and ignore the restored
``credentials.toml``; ``DEVIN_*_API_KEY`` env would bypass the account blob.
Both are stripped from agent child env and unset defensively in
``runtime/entrypoint.sh``.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

CREDENTIAL_ENV = "SBX_ACCOUNT_CREDENTIAL"
ACCOUNT_ID_ENV = "SBX_ACCOUNT_ID"

# Secret material that must never reach a provider CLI child process.
# (``SBX_PROVIDER_API_KEY`` is excluded on purpose: in ``--auth provider`` mode
# the Codex CLI itself reads it via config.toml ``env_key``.)
CREDENTIAL_ENV_EXCLUDE: tuple[str, ...] = ("CODEX_AUTH_JSON", CREDENTIAL_ENV)

# Devin-only (SOR-74): host/Desktop auth bridges and API-key env that would
# override or replace the restored credential blob.
DEVIN_ENV_EXCLUDE: tuple[str, ...] = (
    "ACP_BACKEND",
    "DEVIN_API_KEY",
    "DEVIN_V3_API_KEY",
    "DEVIN_LEGACY_API_KEY",
    "DEVIN_ORG_ID",
    "WINDSURF_API_KEY",
    "DEVIN_OUTPOSTS_TOKEN",
)

# Union stripped by ``scrub_child_env`` for non-Codex agent children.
AGENT_ENV_EXCLUDE: tuple[str, ...] = CREDENTIAL_ENV_EXCLUDE + DEVIN_ENV_EXCLUDE


class CredentialError(Exception):
    """Malformed or provider-mismatched ``SBX_ACCOUNT_CREDENTIAL`` blob."""


def sandbox_home(root: Path) -> Path:
    """``$HOME`` inside the sandbox: ``$SBX_WORK/home`` (filesystem.md)."""
    return root / "home"


def scrub_child_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """``env`` (default ``os.environ``) minus :data:`AGENT_ENV_EXCLUDE`.

    Callers still set ``HOME`` / XDG / ``SBX_WORK`` on the result as needed.
    """
    src = os.environ if env is None else env
    return {k: v for k, v in src.items() if k not in AGENT_ENV_EXCLUDE}


def load_credential_blob(raw: str) -> dict[str, Any]:
    """Parse and validate the ``SBX_ACCOUNT_CREDENTIAL`` JSON blob."""
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CredentialError(f"{CREDENTIAL_ENV} is not valid JSON") from exc
    if not isinstance(blob, dict):
        raise CredentialError(f"{CREDENTIAL_ENV} must be a JSON object")
    provider = blob.get("provider")
    if not isinstance(provider, str) or not provider:
        raise CredentialError(f"{CREDENTIAL_ENV} missing string 'provider'")
    files = blob.get("files")
    if not isinstance(files, dict):
        raise CredentialError(f"{CREDENTIAL_ENV} missing object 'files'")
    for relpath, value in files.items():
        _check_relpath(relpath)
        _decode_file_content(value)  # validate eagerly before writing anything
    return blob


def credential_target(
    home: Path,
    relpath: str,
    *,
    provider: str,
    codex_home: Path | None = None,
) -> Path:
    """Resolve one credential path without allowing it to escape its auth root.

    Contract blob keys are relative to ``$HOME``.  Codex is the compatibility
    exception: ``.codex/*`` must follow the actual ``CODEX_HOME`` used by the
    CLI, which can be outside ``$HOME`` in P1/production.
    """
    _check_relpath(relpath)
    parts = PurePosixPath(relpath).parts
    if provider == "codex" and parts and parts[0] == ".codex" and codex_home is not None:
        base = codex_home.resolve()
        dest = base.joinpath(*parts[1:]).resolve()
        if not dest.is_relative_to(base):
            raise CredentialError(f"credential path escapes CODEX_HOME: {relpath!r}")
        return dest

    base = home.resolve()
    dest = base.joinpath(*parts).resolve()
    if not dest.is_relative_to(base):
        raise CredentialError(f"credential path escapes $HOME: {relpath!r}")
    return dest


def restore_credential_blob(
    home: Path,
    *,
    provider: str,
    env: Mapping[str, str] | None = None,
    codex_home: Path | None = None,
) -> list[Path]:
    """Restore ``SBX_ACCOUNT_CREDENTIAL`` files with mode 600.

    No-op (returns ``[]``) when the env var is unset.  All blob entries and
    destinations are validated before the first write, so malformed input
    cannot leave a partially restored credential set.
    """
    src = os.environ if env is None else env
    raw = src.get(CREDENTIAL_ENV)
    if not raw:
        return []
    blob = load_credential_blob(raw)
    if blob["provider"] != provider:
        raise CredentialError(
            f"credential blob provider {blob['provider']!r} does not match --provider {provider!r}"
        )

    pending: list[tuple[Path, bytes]] = []
    for relpath, value in sorted(blob["files"].items()):
        dest = credential_target(home, str(relpath), provider=provider, codex_home=codex_home)
        pending.append((dest, _decode_file_content(value)))

    written: list[Path] = []
    for dest, content in pending:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        dest.chmod(0o600)
        written.append(dest)
    return written


def _check_relpath(relpath: Any) -> None:
    if not isinstance(relpath, str) or not relpath.strip():
        raise CredentialError(f"invalid credential path: {relpath!r}")
    if "\x00" in relpath or "\\" in relpath:
        raise CredentialError(f"invalid credential path: {relpath!r}")
    parts = PurePosixPath(relpath).parts
    if PurePosixPath(relpath).is_absolute() or not parts or any(p in ("..", ".") for p in parts):
        raise CredentialError(f"credential path escapes $HOME: {relpath!r}")


def _decode_file_content(value: Any) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, dict):
        if "content_b64" in value:
            try:
                return base64.b64decode(str(value["content_b64"]), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise CredentialError("credential file content_b64 is not valid base64") from exc
        if "content" in value:
            return str(value["content"]).encode("utf-8")
    raise CredentialError("credential file entries must be a string or {content|content_b64}")
