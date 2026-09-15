"""``runner init``: config.toml, auth.json, AGENTS.md, empty session.

P2 (SOR-62/SOR-72): provider-aware init. ``--provider`` selects the
``AgentAdapter``; the ``SBX_ACCOUNT_CREDENTIAL`` blob is restored under the
sandbox ``$HOME`` (files mode 600) before ``prepare_home`` runs. Codex keeps
its v1 ``CODEX_AUTH_JSON`` / ``$SBX_WORK/auth.json`` paths.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from runtime.runner import mcp
from runtime.runner.constants import (
    EXIT_INTERNAL,
    EXIT_OK,
    PLACEHOLDER_AUTH_JSON,
    PLACEHOLDER_PROVIDER_AUTH_JSON,
    SANDBOX_AGENTS_MD,
    SHELL_ENV_EXCLUDE,
)
from runtime.runner.credentials import (
    ACCOUNT_ID_ENV,
    CREDENTIAL_ENV,
    CredentialError,
    credential_target,
    load_credential_blob,
    restore_credential_blob,
)
from runtime.runner.workspace import (
    atomic_write,
    codex_home,
    default_session,
    ensure_layout,
    events_path,
    events_raw_path,
    load_session,
    sandbox_home,
    save_session,
    work_root,
)


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_config_toml(*, model: str, auth: str) -> str:
    exclude = ", ".join(_toml_str(item) for item in SHELL_ENV_EXCLUDE)
    lines = [
        f"model = {_toml_str(model)}",
    ]
    if auth == "provider":
        lines.append('model_provider = "sbx"')
    lines += [
        'approval_policy = "never"',
        'sandbox_mode = "danger-full-access"',
        "",
    ]
    if auth == "provider":
        base_url = os.environ.get("SBX_PROVIDER_BASE_URL", "")
        lines += [
            "[model_providers.sbx]",
            'name = "sbx"',
            f"base_url = {_toml_str(base_url)}",
            'env_key = "SBX_PROVIDER_API_KEY"',
            "",
        ]
    lines += [
        "[shell_environment_policy]",
        f"exclude = [{exclude}]",
        "",
    ]
    return "\n".join(lines)


def _write_auth_json(home: Path, auth: str, root: Path) -> None:
    dest = home / "auth.json"
    raw = os.environ.get("CODEX_AUTH_JSON") or ""
    work_auth = root / "auth.json"
    if auth == "auth_json" and raw:
        text = raw if raw.endswith("\n") else raw + "\n"
        dest.write_text(text, encoding="utf-8")
    elif auth == "auth_json" and work_auth.is_file():
        dest.write_bytes(work_auth.read_bytes())
    elif auth == "auth_json":
        dest.write_text(json.dumps(PLACEHOLDER_AUTH_JSON, indent=2) + "\n", encoding="utf-8")
    else:
        dest.write_text(
            json.dumps(PLACEHOLDER_PROVIDER_AUTH_JSON, indent=2) + "\n", encoding="utf-8"
        )
    dest.chmod(0o600)


def _credential_relpaths(provider: str) -> list[str]:
    raw = os.environ.get(CREDENTIAL_ENV)
    if not raw:
        return []
    blob = load_credential_blob(raw)
    if blob["provider"] != provider:
        raise CredentialError(
            f"credential blob provider {blob['provider']!r} does not match --provider {provider!r}"
        )
    return sorted(str(path) for path in blob["files"])


def cmd_init(
    *, auth: str, model: str, provider: str = "codex", account_id: str | None = None
) -> int:
    from runtime.runner.adapter import get_adapter

    root = work_root()
    ensure_layout(root)
    home = codex_home(root)

    try:
        credential_files = _credential_relpaths(provider)
    except CredentialError as exc:
        print(f"runner init: {exc}", file=sys.stderr)
        return EXIT_INTERNAL

    if provider == "codex":
        atomic_write(home / "config.toml", render_config_toml(model=model, auth=auth))
        # Keep the P1 compatibility source, then let the account blob win.
        _write_auth_json(home, auth, root)
    try:
        restore_credential_blob(
            sandbox_home(root),
            provider=provider,
            codex_home=home if provider == "codex" else None,
        )
    except CredentialError as exc:
        print(f"runner init: {exc}", file=sys.stderr)
        return EXIT_INTERNAL

    if provider != "codex":
        adapter = get_adapter(provider)
        adapter.prepare_home(sandbox_home(root), model)

    atomic_write(root / "AGENTS.md", SANDBOX_AGENTS_MD)
    events_path(root).write_text("", encoding="utf-8")
    events_raw_path(root).write_text("", encoding="utf-8")
    session = default_session()
    session["model"] = model
    session["auth"] = auth
    session["provider"] = provider
    session["account_id"] = account_id or os.environ.get(ACCOUNT_ID_ENV)
    session["credential_files"] = credential_files
    session["mcp_servers"] = (
        [mcp.LINEAR_SERVER_NAME] if provider == "devin" and mcp.linear_mcp_enabled() else []
    )
    save_session(root, session)
    return EXIT_OK


def cmd_export_credentials() -> int:
    """Print the refreshed credential blob to stdout; empty when unchanged.

    Reads the files recorded in ``session.json.credential_files`` (restored at
    init) back out of the sandbox ``$HOME`` and compares against the injected
    ``SBX_ACCOUNT_CREDENTIAL`` blob.
    """
    root = work_root()
    session = load_session(root)
    relpaths = session.get("credential_files") or []
    provider = str(session.get("provider") or "codex")
    files: dict[str, str] = {}
    for relpath in relpaths:
        relpath = str(relpath)
        try:
            target = credential_target(
                sandbox_home(root),
                relpath,
                provider=provider,
                codex_home=codex_home(root) if provider == "codex" else None,
            )
        except CredentialError:
            continue
        if target.is_file():
            files[relpath] = target.read_text(encoding="utf-8")
    if not files:
        return EXIT_OK
    new_blob = {"provider": session.get("provider") or "codex", "files": files}
    old_raw = os.environ.get("SBX_ACCOUNT_CREDENTIAL")
    if old_raw:
        try:
            old_blob = json.loads(old_raw)
        except json.JSONDecodeError:
            old_blob = None
        if old_blob == new_blob:
            return EXIT_OK
    print(json.dumps(new_blob, ensure_ascii=False))
    return EXIT_OK
