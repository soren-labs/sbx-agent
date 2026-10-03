"""Explicit browser-origin contract for the static frontend / VPS topology."""

from urllib.parse import urlsplit

from fastapi.middleware.cors import CORSMiddleware


def configure_browser_origins(app, value: str):
    origins = tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    for origin in origins:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or "*" in origin
        ):
            raise ValueError("SBX_BROWSER_ORIGINS must contain exact HTTP(S) origins")
    app.state.browser_origins = origins
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "Authorization", "Last-Event-ID", "Idempotency-Key"],
            expose_headers=["Retry-After"],
            max_age=600,
        )
