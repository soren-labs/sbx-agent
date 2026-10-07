"""Modal deployment of the sbx-agent documentation site.

Build the static site first (``make docs-build`` or
``npm --prefix docs-site run build``), then deploy with::

    uv run modal deploy docs-site/modal_deploy.py

The app serves ``docs-site/dist`` as static files. Pointing a custom domain
(e.g. ``docs.sorenforge.com``) at the printed ``*.modal.run`` URL is a DNS /
custom-domain step on the Modal workspace.
"""

from pathlib import Path

import modal

DIST = Path(__file__).resolve().parent / "dist"

app = modal.App("sbx-docs")

image = (
    modal.Image.debian_slim()
    .pip_install("starlette", "uvicorn")
    .add_local_dir(str(DIST), remote_path="/site", copy=True)
)


@app.function(image=image, min_containers=1)
@modal.asgi_app(label="docs")
def web():
    from starlette.applications import Starlette
    from starlette.responses import FileResponse
    from starlette.staticfiles import StaticFiles

    async def not_found(_request, _exc):
        return FileResponse("/site/404.html", status_code=404)

    site = Starlette(exception_handlers={404: not_found})
    site.mount("/", StaticFiles(directory="/site", html=True), name="site")
    return site
