"""FastAPI application assembly for the single business API."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from control.api import auth, errors, live, resources, work


def create_api(services: Any, *, extra_routers: list[Any] | None = None) -> FastAPI:
    app = FastAPI(
        title="SBX API",
        version="2.0.0",
        openapi_url="/api/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.state.services = services
    errors.install(app)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        errors.request_id(request)
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    # Build revision baked into the image (Dockerfile SBX_GIT_SHA); lets a deployment be
    # matched to the commit it was built from. Not a secret; absent in source checkouts.
    revision = os.environ.get("SBX_GIT_SHA") or None
    git_sha = None if revision == "unknown" else revision

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "git_sha": git_sha}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        try:
            services.tx.read(lambda uow: uow.now())
        except Exception:
            return JSONResponse({"ok": False}, status_code=503)
        return JSONResponse({"ok": True})

    app.include_router(auth.router)
    app.include_router(resources.router)
    app.include_router(work.router)
    app.include_router(live.router)
    for router in extra_routers or []:
        app.include_router(router)
    return app
