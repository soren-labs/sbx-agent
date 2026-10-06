"""Optional Codex native auth upload: allowlisted format validation only."""

from __future__ import annotations

import json
from typing import Any

from control.domain.errors import DomainError
from control.integrations.connectors.base import Observation

KIND = "codex"
FORMAT = "codex_auth_json/v1"
MAX_BYTES = 64 * 1024
_ALLOWED_TOP = {"OPENAI_API_KEY", "tokens", "last_refresh", "auth_mode"}


def normalize(credential: dict[str, Any]) -> dict[str, Any]:
    raw = credential.get("auth_json")
    if not isinstance(raw, str) or not raw or len(raw.encode()) > MAX_BYTES:
        raise DomainError(
            "validation_failed",
            "auth_json must be a bounded JSON document",
            details={"field": "credential.auth_json"},
        )
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise DomainError(
            "validation_failed",
            "auth_json is not valid JSON",
            details={"field": "credential.auth_json"},
        ) from exc
    if (
        not isinstance(parsed, dict)
        or not set(parsed) <= _ALLOWED_TOP
        or not (parsed.get("tokens") or parsed.get("OPENAI_API_KEY"))
    ):
        raise DomainError(
            "validation_failed",
            "unsupported Codex auth format; refused",
            details={"field": "credential.auth_json"},
        )
    return {"auth_json": json.dumps(parsed, separators=(",", ":"))}


def validate(credential: dict[str, Any], **_: Any) -> Observation:
    return Observation(
        "ready", details={"probe": "format_only", "note": "no provider probe; first Turn verifies"}
    )
