"""Codex subscription adapter: official ``codex login --device-auth`` in a Setup VM."""

from __future__ import annotations

from typing import Any

from control.integrations.subscriptions.base import PROFILE_MOUNT, SubscriptionAdapter

# File-backed auth keeps the login on the Slot Volume (a VM has no OS keyring).
_CODEX = ["codex", "-c", 'cli_auth_credentials_store="file"']
_WORK = "/tmp/sbx-setup/verify"


def _text(value: Any, limit: int) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def parse_catalog(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize ``model/list`` items. Every id and effort is the CLI's own; none is invented."""
    models = []
    for item in raw.get("items") or []:
        model_id = _text(item.get("model") or item.get("id"), 120)
        if not model_id or item.get("hidden"):
            continue
        efforts = []
        for option in item.get("supportedReasoningEfforts") or []:
            effort = _text(
                option.get("reasoningEffort") if isinstance(option, dict) else option, 40
            )
            if effort and effort not in [e["id"] for e in efforts]:
                description = option.get("description") if isinstance(option, dict) else None
                efforts.append({"id": effort, "description": _text(description, 200)})
        default_effort = _text(item.get("defaultReasoningEffort"), 40)
        models.append(
            {
                "id": model_id,
                "name": _text(item.get("displayName"), 120) or model_id,
                "description": _text(item.get("description"), 300),
                "default": bool(item.get("isDefault")),
                "reasoning": {
                    "kind": "levels" if efforts else "none",
                    "efforts": efforts,
                    "default": default_effort
                    if default_effort in [e["id"] for e in efforts]
                    else None,
                },
            }
        )
    if not models:
        return None
    default = next((m["id"] for m in models if m["default"]), None)
    return {"models": models, "default_model": default, "complete": bool(raw.get("complete"))}


ADAPTER = SubscriptionAdapter(
    provider_id="codex",
    display_name="Codex",
    harness_provider_id="codex",
    login_method="device_code",
    verification_host="auth.openai.com",
    profile_env={
        "HOME": PROFILE_MOUNT,
        "CODEX_HOME": f"{PROFILE_MOUNT}/.codex",
        "XDG_CONFIG_HOME": f"{PROFILE_MOUNT}/.config",
        "XDG_DATA_HOME": f"{PROFILE_MOUNT}/.local/share",
    },
    catalog_source="codex app-server model/list",
    parse_catalog=parse_catalog,
    setup={
        # The CLI's own machine interface lists the models this login may use.
        "catalog": {
            "argv": [*_CODEX, "app-server"],
            "requests": [
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "sbx", "title": "SBX", "version": "1"}},
                },
                {"method": "initialized"},
                {"id": 2, "method": "model/list", "params": {}},
            ],
            "result_id": 2,
            "items_key": "data",
            "cursor_key": "nextCursor",
            "item_fields": [
                "id",
                "model",
                "displayName",
                "description",
                "isDefault",
                "hidden",
                "defaultReasoningEffort",
                "supportedReasoningEfforts",
            ],
            "timeout": 60,
        },
        # The CLI does not create its home on a fresh Volume.
        "ensure_dirs": [f"{PROFILE_MOUNT}/.codex"],
        "login_argv": [*_CODEX, "login", "--device-auth"],
        "status_argv": [*_CODEX, "login", "status"],
        "status_ok": "Logged in using ChatGPT",
        "version_argv": ["codex", "--version"],
        # One real, tool-less model call proves the login is usable, not just present.
        "verify_argv": [
            *_CODEX,
            "--disable",
            "shell_tool",
            "exec",
            "--ephemeral",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-C",
            _WORK,
            "Do not use tools or read any files. Reply with exactly: SBX_SLOT_OK",
        ],
        "verify_ok": '"turn.completed"',
        "verify_cwd": _WORK,
        "url_pattern": r"https://auth\.openai\.com/\S+",
        "code_pattern": r"\b[A-Z0-9]{4,6}-[A-Z0-9]{4,6}\b",
        "expiry_pattern": r"(?:expires? in|valid for)\s+(\d+)\s+minutes?",
        "default_code_seconds": 900,
    },
)
