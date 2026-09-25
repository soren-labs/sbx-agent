"""Runtime-derived public OpenAPI for ``/v1`` (SOR-226).

``GET /v1/openapi.json`` serves a spec generated from the live FastAPI
routes and Pydantic request models — never a hand-maintained copy that can
drift from the implementation. On top of the generated operation surface the
builder injects the canonical contract pieces: the ``ErrorBody`` schema
(with the catalog's full code/action enums), the bearer security scheme and
the ``x-canonical`` block (error catalog, providers, SSE + runner
constants).

``docs/contracts/api-v1.yaml`` stays the normative declaration;
``tests/unit/api_v1/test_openapi_parity.py`` asserts the generated spec and
the contract agree on paths, methods, error codes and the error body shape.
"""

from __future__ import annotations

from typing import Any, get_args

from fastapi import APIRouter
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

from control.api_v1.error_catalog import (
    ERROR_ACTIONS,
    ERROR_CATALOG,
    ERROR_SUBCODES,
)
from control.api_v1.schemas import ProviderId
from control.run_errors import RUN_ERROR_CODES, RUN_ERROR_SOURCES

TITLE = "sbx-browser Public API v1"
VERSION = "1.0.0"
KEEPALIVES_S = 15

_INFO_DESCRIPTION = """\
公开 REST API，形状对齐 Cursor Cloud Agents API（设计 v2 §3.4）。
`agent ≙ session`，`run ≙ turn`：创建 agent 立刻跑首个 run，follow-up
是同一 agent 上的新 run。
认证：`Authorization: Bearer sbx_<key>`（控制面只存 `sha256(key)`）。
scopes：`agents`（默认）/ `admin`（accounts / api-keys / verify 需要）。
错误体统一 `{error:{code, message, retryable, action, retry_after?, details?}}`；
`code` 取 canonical `error_subcodes`，HTTP 状态取 canonical `error_codes`。
Run 终态另带结构化 `error`（`RunError`：`code/source/message/retryable/
retry_after?`），`code` 取 canonical `run_error_codes`，`source` 取
`run_error_sources`；调用方无需解析 SSE 即可诊断失败主因。
SSE 帧与 `/api/*` 相同：`id: <events.jsonl 行号>` / `event: <type>` /
`data: <json>`，15 s `: keepalive`，支持 `Last-Event-ID` 续传。
"""


def error_body_schema() -> dict[str, Any]:
    """Canonical ``ErrorBody`` component — SOR-226 shape."""
    return {
        "type": "object",
        "required": ["error"],
        "properties": {
            "error": {
                "type": "object",
                "required": ["code", "message", "retryable", "action"],
                "properties": {
                    "code": {"type": "string", "enum": list(ERROR_SUBCODES)},
                    "message": {"type": "string"},
                    "retryable": {
                        "type": "boolean",
                        "description": (
                            "Whether the same request may be retried unchanged "
                            "(honor `retry_after` when present)."
                        ),
                    },
                    "action": {
                        "type": "string",
                        "enum": list(ERROR_ACTIONS),
                        "description": (
                            "Client action hint: `authenticate` (fix credentials), "
                            "`lookup` (verify the referenced id), `fix_request` "
                            "(change the request), `wait` (retry after the busy "
                            "object settles), `retry` (transient; resend), "
                            "`configure` (operator setup required)."
                        ),
                    },
                    "retry_after": {
                        "type": "number",
                        "description": (
                            "Seconds; present on `provider_exhausted` / "
                            "`concurrency_limit` / `task_active` when known."
                        ),
                    },
                    "details": {
                        "type": "object",
                        "description": (
                            "Optional structured context — e.g. `loc` lists the "
                            "request fields that failed validation."
                        ),
                    },
                },
            }
        },
    }


def _error_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorBody"}}},
    }


def x_canonical() -> dict[str, Any]:
    """The ``x-canonical`` block — runtime-derived where code defines it.

    Error catalog data comes from :mod:`control.api_v1.error_catalog`;
    provider/runner constants mirror the frozen runner-side contract
    (``docs/contracts/api-v1.yaml`` asserts the same values).
    """
    return {
        "error_codes": sorted({spec.status for spec in ERROR_CATALOG.values()}),
        "run_error_codes": list(RUN_ERROR_CODES),
        "run_error_sources": list(RUN_ERROR_SOURCES),
        "error_subcodes": list(ERROR_SUBCODES),
        "providers": sorted(get_args(ProviderId)),
        "keepalives_s": KEEPALIVES_S,
        "sse": {
            "id": "events.jsonl line number",
            "event": "type",
            "data": "json",
            "keepalive": ": keepalive",
        },
        "paths": [
            "inbox/<n>.md",
            "turns/<n>.json",
            "events.jsonl",
            "events.raw.jsonl",
            "session.json",
        ],
        "exit_codes": {
            "0": "success",
            "2": "cli_nonzero",
            "3": "timeout",
            "4": "bad_json",
            "5": "auth_invalid",
        },
        "commands": ["init", "turn", "stop", "export-credentials"],
        "codex_events": [
            "thread.started",
            "turn.started",
            "item.started",
            "item.updated",
            "item.completed",
            "turn.completed",
            "turn.failed",
            "error",
        ],
        "item_types": ["agent_message", "command_execution", "file_change", "reasoning", "error"],
        "runner_events": ["sbx.turn_started", "sbx.turn_finished", "sbx.error", "sbx.session_meta"],
        "usage_fields": ["input_tokens", "cached_input_tokens", "output_tokens"],
        "usage_fields_optional": ["cache_write_input_tokens", "reasoning_output_tokens"],
        "error_actions": list(ERROR_ACTIONS),
        "error_status_by_code": {code: spec.status for code, spec in sorted(ERROR_CATALOG.items())},
    }


def build_v1_openapi(router: APIRouter) -> dict[str, Any]:
    """Generate the public spec from the live ``/v1`` router."""
    routes = [r for r in router.routes if isinstance(r, APIRoute) and r.include_in_schema]
    spec = get_openapi(
        title=TITLE,
        version=VERSION,
        openapi_version="3.1.0",
        description=_INFO_DESCRIPTION,
        routes=routes,
        servers=[{"url": "https://sbx.sorenforge.com", "description": "Production"}],
    )
    # FastAPI emits a 422 HTTPValidationError response for routes with typed
    # inputs; V1Route maps validation failures to 400 + the canonical body.
    for path_item in spec.get("paths", {}).values():
        for operation in path_item.values():
            responses = operation.get("responses")
            if isinstance(responses, dict) and responses.pop("422", None) is not None:
                responses.setdefault("400", _error_response("malformed request"))
    components = spec.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)
    schemas["ErrorBody"] = error_body_schema()
    components["securitySchemes"] = {"bearerAuth": {"type": "http", "scheme": "bearer"}}
    spec["security"] = [{"bearerAuth": []}]
    spec["x-canonical"] = x_canonical()
    return spec


def register(router: APIRouter) -> None:
    """Attach ``GET /v1/openapi.json`` to the shared v1 router."""

    @router.get("/openapi.json")
    def v1_openapi_json() -> dict[str, Any]:
        """The generated public OpenAPI document — runtime-derived, never stale."""
        return build_v1_openapi(router)
