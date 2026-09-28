"""Public REST API v2 — the Session-first product surface (SOR-256).

A Session is the only resource a V2 client names. Under the hood a session
is a durable :class:`control.tasks.TaskRecord` (``sess_``-prefixed id) bound
to the existing Task/Agent/Run/Workspace/Revision engine — the facade adds
a bounded first-view projection, sanitized responses, and an SSE stream
normalized to Session-vocabulary events. V1 keeps working untouched.

Endpoints are registered by :mod:`control.api_v2.routes`; every route uses
:class:`~control.api_v1.errors.V1Route` so failures surface the same
canonical ``{error: {code, message, retryable, action, ...}}`` body as /v1.
``GET /v2/openapi.json`` serves the spec generated from these routes.
"""

from __future__ import annotations

from fastapi import APIRouter

from control.api_v1.errors import V1Route

router = APIRouter(prefix="/v2", route_class=V1Route)

from control.api_v2 import routes as _routes  # noqa: E402,F401
from control.api_v2.openapi import register as _register_openapi  # noqa: E402

_register_openapi(router)
