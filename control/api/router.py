"""FastAPI application assembly for the single business API."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from control.api import auth, errors, resources, work


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

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True}

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
    for router in extra_routers or []:
        app.include_router(router)
    return app
