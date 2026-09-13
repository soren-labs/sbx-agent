"""Local static + API proxy so Playwright can talk to deployed sbx-control."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}


def _auth() -> tuple[str, str]:
    user = os.environ.get("SBX_BASIC_USER") or os.environ.get("SBX_API_USER") or ""
    password = os.environ.get("SBX_BASIC_PASS") or os.environ.get("SBX_API_PASSWORD") or ""
    if not user or not password:
        raise SystemExit("SBX_BASIC_USER / SBX_BASIC_PASS required")
    return user, password


def _control() -> str:
    url = (os.environ.get("SBX_CONTROL_URL") or "").strip().rstrip("/")
    if not url:
        from tests.e2e_modal.helpers import discover_control_url

        url = discover_control_url()
    return url


def build_app() -> FastAPI:
    control = _control()
    auth = _auth()
    app = FastAPI(title="sbx-e2e-ui-proxy")

    @app.get("/e2e-config.js")
    def e2e_config() -> Response:
        user, password = auth
        body = (
            f"window.SBX_API_BASE = '';\n"
            f"window.SBX_API_USER = {json.dumps(user)};\n"
            f"window.SBX_API_PASSWORD = {json.dumps(password)};\n"
        )
        return Response(body, media_type="application/javascript")

    def _index_html() -> str:
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        needle = '<script type="module" src="./app.js"></script>'
        inject = (
            '<script src="/e2e-config.js"></script>\n'
            '    <script type="module" src="./app.js"></script>'
        )
        if needle not in html:
            raise RuntimeError("web/index.html missing app.js script tag")
        return html.replace(needle, inject)

    @app.get("/")
    def index() -> HTMLResponse:
        return HTMLResponse(_index_html())

    @app.get("/index.html")
    def index_html() -> HTMLResponse:
        return HTMLResponse(_index_html())

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy_api(path: str, request: Request) -> StreamingResponse:
        target = f"{control}/api/{path}"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in HOP_BY_HOP and key.lower() != "authorization"
        }
        body = await request.body()
        client = httpx.AsyncClient(timeout=None, auth=auth, follow_redirects=True)
        req = client.build_request(request.method, target, headers=headers, content=body or None)
        resp = await client.send(req, stream=True)
        out_headers = {
            key: value for key, value in resp.headers.items() if key.lower() not in HOP_BY_HOP
        }
        media_type = resp.headers.get("content-type")
        if media_type and "text/event-stream" in media_type:
            out_headers["Cache-Control"] = "no-cache"
            out_headers["X-Accel-Buffering"] = "no"
            out_headers.pop("content-length", None)
            out_headers.pop("Content-Length", None)

        async def gen():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            gen(),
            status_code=resp.status_code,
            headers=out_headers,
            media_type=media_type,
        )

    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return app


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_background(port: int | None = None) -> tuple[uvicorn.Server, threading.Thread, str]:
    port = port or int(os.environ.get("SBX_UI_PORT") or 0) or free_port()
    server = uvicorn.Server(
        uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True, name="sbx-e2e-ui")
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline_ok = False
    import time

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            httpx.get(base + "/", timeout=0.3)
            deadline_ok = True
            break
        except httpx.TransportError:
            time.sleep(0.05)
    if not deadline_ok:
        server.should_exit = True
        raise RuntimeError("ui proxy did not start")
    return server, thread, base


def main() -> None:
    parser = argparse.ArgumentParser(description="WP2-H UI proxy for deployed sbx-control")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    os.chdir(REPO_ROOT)
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
