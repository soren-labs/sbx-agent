"""Grok Build subscription adapter: official ``grok login --device-auth`` in a Setup VM.

Verified against Grok Build 1.0.50: the device login prints a ``accounts.x.ai`` URL and a
one-time code and stores the session under ``$GROK_HOME`` (file-backed, no OS keyring).
The CLI has no login-status command; ``grok models`` says ``You are not authenticated``
while signed out and exits 0, so its words decide.
"""

from __future__ import annotations

from typing import Any

from control.integrations.subscriptions.base import PROFILE_MOUNT, SubscriptionAdapter

# Never self-update inside a VM: the image pins the verified version.
_GROK = ["grok", "--no-auto-update"]
_WORK = "/tmp/sbx-setup/verify"


def _text(value: Any, limit: int) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def parse_catalog(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize ``modelState.availableModels``. Every id and effort is the CLI's own.

    An effort's id is the model's menu option id, which is what ``--effort`` accepts.
    """
    current = _text(raw.get("current"), 120)
    models = []
    for item in raw.get("items") or []:
        model_id = _text(item.get("modelId"), 120)
        if not model_id:
            continue
        meta = item.get("_meta") if isinstance(item.get("_meta"), dict) else {}
        efforts: list[dict[str, Any]] = []
        default_effort = None
        options = meta.get("reasoningEfforts") if meta.get("supportsReasoningEffort") else None
        for option in options if isinstance(options, list) else []:
            effort = _text(option.get("id"), 40) if isinstance(option, dict) else None
            if not effort or effort in [e["id"] for e in efforts]:
                continue
            description = _text(option.get("description"), 200) or _text(option.get("label"), 200)
            efforts.append({"id": effort, "description": description})
            if option.get("default") is True:
                default_effort = effort
        models.append(
            {
                "id": model_id,
                "name": _text(item.get("name"), 120) or model_id,
                "description": _text(item.get("description"), 300),
                "default": model_id == current,
                "reasoning": {
                    "kind": "levels" if efforts else "none",
                    "efforts": efforts,
                    "default": default_effort,
                },
            }
        )
    if not models:
        return None
    default = current if current in [m["id"] for m in models] else None
    return {"models": models, "default_model": default, "complete": bool(raw.get("complete"))}


ADAPTER = SubscriptionAdapter(
    provider_id="grok",
    display_name="Grok Build",
    harness_provider_id="grok",
    login_method="device_code",
    verification_host="accounts.x.ai",
    profile_env={
        "HOME": PROFILE_MOUNT,
        "GROK_HOME": f"{PROFILE_MOUNT}/.grok",
        "XDG_CONFIG_HOME": f"{PROFILE_MOUNT}/.config",
        "XDG_DATA_HOME": f"{PROFILE_MOUNT}/.local/share",
    },
    catalog_source="grok agent stdio initialize",
    parse_catalog=parse_catalog,
    setup={
        # The CLI's own agent protocol reports the models and each model's effort menu.
        "catalog": {
            "argv": [*_GROK, "agent", "stdio"],
            "requests": [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": 1, "clientCapabilities": {}},
                }
            ],
            "result_id": 1,
            "items_path": ["_meta", "modelState", "availableModels"],
            "value_paths": {"current": ["_meta", "modelState", "currentModelId"]},
            "item_fields": ["modelId", "name", "description", "_meta"],
            "timeout": 60,
        },
        "ensure_dirs": [f"{PROFILE_MOUNT}/.grok"],
        "login_argv": ["grok", "login", "--device-auth"],
        "status_argv": [*_GROK, "models"],
        "status_ok": "Available models",
        "status_fail": ["not authenticated", "not signed in"],
        "version_argv": ["grok", "--version"],
        # One real, tool-less model call proves the login is usable, not just present.
        "verify_argv": [
            *_GROK,
            "-p",
            "Do not use tools or read any files. Reply with exactly: SBX_SLOT_OK",
            "--output-format",
            "json",
            "--max-turns",
            "1",
            "--disable-web-search",
            "--cwd",
            _WORK,
        ],
        "verify_ok": '"stopReason"',
        "verify_cwd": _WORK,
        "url_pattern": r"https://accounts\.x\.ai/\S+",
        "code_pattern": r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b",
        "expiry_pattern": r"(?:expires? in|valid for)\s+(\d+)\s+minutes?",
        "default_code_seconds": 900,
    },
)
