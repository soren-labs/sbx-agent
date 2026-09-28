"""Runtime-derived public OpenAPI for ``/v2`` (SOR-256).

Same derivation discipline as ``/v1``: the spec is generated from the live
routes + strict Pydantic models — never a hand-maintained copy. On top the
builder injects the canonical ``ErrorBody`` (shared catalog), bearer auth and
a ``x-canonical`` block declaring the V2 status/phase/event vocabulary.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

from control.api_v1.openapi import _error_response, error_body_schema
from control.api_v2.schemas import DELIVERY_MODES, EVENT_TYPES, SESSION_PHASES, SESSION_STATUSES

TITLE = "sbx-browser Public API v2"
VERSION = "2.0.0"
KEEPALIVES_S = 15

_INFO_DESCRIPTION = """\
Session-first REST API facade over the SBX engine (SOR-256).
The public product object is `Session` — Task/Agent/Run/Revision ids never
appear in responses. Auth + error contract are identical to /v1:
`Authorization: Bearer sbx_<key>`, `agents` scope, and
`{error:{code, message, retryable, action, retry_after?, details?}}`.
Writes are ACK-fast: create/messages/cancel/retry/deliver never block on
Modal/provider/GitHub work — they persist durable intent and return the
session projection; watch `GET /v2/sessions/{id}/events` (SSE) for progress.
SSE frames: `id: <session-scoped seq>` / `event: <type>` / `data: <json>`,
`Last-Event-ID` resumes from the bounded replay buffer, `: keepalive` heartbeats.
"""


def x_canonical() -> dict[str, Any]:
    """The V2 contract vocabulary — runtime-derived constants."""
    return {
        "session_statuses": list(SESSION_STATUSES),
        "session_phases": list(SESSION_PHASES),
        "delivery_modes": list(DELIVERY_MODES),
        "event_types": list(EVENT_TYPES),
        "sse": {
            "id": "per-session event sequence",
            "event": "type",
            "data": "json",
            "keepalive": ": keepalive",
            "replay": "bounded buffer; Last-Event-ID resumes",
        },
        "keepalives_s": KEEPALIVES_S,
    }


def build_v2_openapi(router: APIRouter) -> dict[str, Any]:
    """Generate the public spec from the live ``/v2`` router."""
    routes = [r for r in router.routes if isinstance(r, APIRoute) and r.include_in_schema]
    spec = get_openapi(
        title=TITLE,
        version=VERSION,
        openapi_version="3.1.0",
        description=_INFO_DESCRIPTION,
        routes=routes,
        servers=[{"url": "https://sbx.sorenforge.com", "description": "Production"}],
    )
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
    """Attach ``GET /v2/openapi.json`` to the shared v2 router."""

    @router.get("/openapi.json")
    def v2_openapi_json() -> dict[str, Any]:
        """The generated public OpenAPI document — runtime-derived, never stale."""
        return build_v2_openapi(router)
