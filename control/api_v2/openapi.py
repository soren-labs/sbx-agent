"""Runtime-derived public OpenAPI for ``/v2`` (SOR-256).

``GET /v2/openapi.json`` serves a spec generated from the live ``/v2``
router and its Pydantic models — never a hand-maintained copy. On top of
the generated surface the builder injects the canonical ``ErrorBody``
component (shared with /v1), the bearer scheme, and an ``x-canonical``
block listing the stable session vocabulary (statuses, phases, event
types).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

from control.api_v1.error_catalog import ERROR_ACTIONS, ERROR_SUBCODES
from control.api_v1.openapi import _error_response, error_body_schema
from control.api_v2.events import SESSION_EVENT_TYPES

TITLE = "sbx-browser Sessions API v2"
VERSION = "2.0.0"
KEEPALIVES_S = 15

_INFO_DESCRIPTION = """\
Session-first public API. A Session is the only resource a client names:
POST creates it and queues the first turn, POST messages queues follow-up
turns, GET events streams normalized Session activity over SSE
(`Last-Event-ID` resume), changes/deliver publish the produced work.
Auth: `Authorization: Bearer sbx_<key>` (scope `agents`).
Errors use the canonical `/v1` body `{error:{code,message,retryable,action,...}}`.
"""


def x_canonical() -> dict[str, Any]:
    """The ``x-canonical`` block — the stable V2 vocabulary."""
    return {
        "session_statuses": ["queued", "running", "finished", "failed", "cancelled"],
        "session_phases": [
            "provisioning",
            "queued",
            "running",
            "delivering",
            "finished",
            "failed",
            "cancelled",
        ],
        "session_event_types": list(SESSION_EVENT_TYPES),
        "sse": {
            "id": "events.jsonl line number",
            "event": "type",
            "data": "json",
            "keepalive": ": keepalive",
            "resume": "Last-Event-ID",
        },
        "error_subcodes": list(ERROR_SUBCODES),
        "error_actions": list(ERROR_ACTIONS),
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
    # V1Route maps validation failures to 400 + the canonical body, so the
    # generated 422 responses are rewritten the same way /v1 does it.
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

    @router.get("/openapi.json", include_in_schema=False)
    def v2_openapi_json() -> dict[str, Any]:
        """The generated public OpenAPI document — runtime-derived, never stale."""
        return build_v2_openapi(router)
