"""Public REST API v2 — Session-first facade (SOR-256).

``Session`` is the caller-facing product object: a typed projection over the
existing Task/Agent/Run/Workspace/Revision/Delivery engine. Session ids are
opaque — internally they map to durable Task records, so no data migration is
needed and every existing V1 behavior stays backward compatible.

Endpoints are registered by ``control.api_v2.routes``; every route uses
:class:`~control.api_v1.errors.V1Route` so failures surface as the canonical
``{error: {code, message, retryable, action, retry_after?, details?}}`` body —
the same error contract ``/v1`` serves. ``GET /v2/openapi.json`` serves the
spec generated from these very routes.
"""

from __future__ import annotations

from fastapi import APIRouter

from control.api_v1.errors import V1Route

router = APIRouter(prefix="/v2", route_class=V1Route)

from control.api_v2 import routes as _routes  # noqa: E402,F401
from control.api_v2.openapi import register as _register_openapi  # noqa: E402

_register_openapi(router)
