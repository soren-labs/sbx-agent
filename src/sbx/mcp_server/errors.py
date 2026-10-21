"""Structured tool errors: public SBX codes and request ids, never secrets or raw bodies."""

from __future__ import annotations

from typing import Any

from mcp import types
from pydantic import ValidationError

from sbx.sdk.errors import SBXError

# Same four categories as `sbx status` (B1): config | reachability | authentication | executor.
CATEGORY = {
    "config_invalid": "config",
    "unauthenticated": "authentication",
    "forbidden": "authentication",
    "csrf_failed": "authentication",
    "transport_error": "reachability",
    "connection_required": "executor",
    "connection_revoked": "executor",
    "unsupported_capability": "executor",
    "executor_unavailable": "executor",
    "validation_failed": "request",
    "result_too_large": "request",
}

ACTIONS = {
    "config_invalid": "Fix the client configuration file (see `sbx auth login`) and retry.",
    "unauthenticated": "Run `sbx auth login`, or set SBX_API_KEY, then restart the MCP client.",
    "transport_error": "Check SBX_BASE_URL and run `sbx status`; start the server with "
    "`sbx server start` for a self-hosted install.",
    "outcome_unknown": "Call sbx_acknowledge_unknown for this turn before sending new work.",
    "result_too_large": "Lower the limit (or max_chars) and call again.",
    "validation_failed": "Correct the arguments listed in details.fields and retry.",
}


def tool_error(
    code: str,
    message: str,
    *,
    action: str | None = None,
    status: int | None = None,
    request_id: str | None = None,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> types.CallToolResult:
    """An MCP tool-level failure (isError) with the same fields for every entry point."""
    body = {
        "code": code,
        "category": CATEGORY.get(code, "api"),
        "message": message,
        "status": status,
        "request_id": request_id,
        "retryable": retryable,
        "action": action or ACTIONS.get(code),
        "details": details or {},
    }
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{code}: {message}")],
        structured_content=body,
        is_error=True,
    )


def from_sbx(exc: SBXError) -> types.CallToolResult:
    return tool_error(
        exc.code,
        exc.message,
        status=exc.status,
        request_id=exc.request_id,
        retryable=exc.retryable,
        action=exc.action,
        details=exc.details,
    )


def from_validation(exc: ValidationError) -> types.CallToolResult:
    """Field locations and our own messages only: pydantic's input echo is never forwarded."""
    fields = [".".join(str(part) for part in err["loc"]) for err in exc.errors()]
    messages = sorted({str(err["msg"]) for err in exc.errors()})
    return tool_error(
        "validation_failed",
        "; ".join(messages) or "arguments are invalid",
        details={"fields": fields},
    )
