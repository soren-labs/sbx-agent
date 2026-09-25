"""Public REST API v1 (Cursor Cloud Agents shape, ``docs/contracts/api-v1.yaml``).

Endpoints are registered by ``control.api_v1.routes`` (P2-D / SOR-64); every
route uses :class:`~control.api_v1.errors.V1Route` so failures surface as the
canonical ``{error: {code, message, retryable, action, retry_after?, details?}}``
body (SOR-226). ``GET /v1/openapi.json`` serves the spec generated from these
very routes plus the canonical error contract.
"""

from __future__ import annotations

from fastapi import APIRouter

from control.api_v1.errors import V1Route

router = APIRouter(prefix="/v1", route_class=V1Route)

from control.api_v1 import revisions as _revisions  # noqa: E402,F401
from control.api_v1 import routes as _routes  # noqa: E402,F401
from control.api_v1 import tasks as _tasks  # noqa: E402,F401
from control.api_v1.openapi import register as _register_openapi  # noqa: E402

_register_openapi(router)
