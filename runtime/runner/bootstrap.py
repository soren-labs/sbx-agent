"""``runner init``: config.toml, auth.json, AGENTS.md, empty session."""

from __future__ import annotations

import json
import os
from pathlib import Path

from runtime.runner.constants import (
    EXIT_OK,
    PLACEHOLDER_AUTH_JSON,
    PLACEHOLDER_PROVIDER_AUTH_JSON,
    SANDBOX_AGENTS_MD,
    SHELL_ENV_EXCLUDE,
)
from runtime.runner.workspace import (
    atomic_write,
    codex_home,
    default_session,
    ensure_layout,
    events_path,
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


def cmd_init(*, auth: str, model: str) -> int:
    root = work_root()
    ensure_layout(root)
    home = codex_home(root)
    atomic_write(home / "config.toml", render_config_toml(model=model, auth=auth))
    _write_auth_json(home, auth, root)
    atomic_write(root / "AGENTS.md", SANDBOX_AGENTS_MD)
    events_path(root).write_text("", encoding="utf-8")
    session = default_session()
    session["model"] = model
    session["auth"] = auth
    save_session(root, session)
    return EXIT_OK
