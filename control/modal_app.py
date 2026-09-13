"""Modal App entry: ASGI + reaper cron.

Tests import ``control.app`` only. This module imports ``modal`` and must not
be imported by unit/integration tests (no Modal connection).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import modal

from control.app import create_app
from control.config import BASIC_SECRET_NAME, CODEX_SECRET_NAME, MODAL_APP_NAME
from control.reaper import reap

app = modal.App(os.environ.get("SBX_MODAL_APP_NAME", MODAL_APP_NAME))

CONTROL_IMAGE = modal.Image.debian_slim(python_version="3.12").pip_install(
    "fastapi",
    "httpx",
    "pydantic",
    "uvicorn",
    "anyio",
    "starlette",
)

_secrets = [
    modal.Secret.from_name(CODEX_SECRET_NAME),
    modal.Secret.from_name(BASIC_SECRET_NAME),
]


@app.function(image=CONTROL_IMAGE, secrets=_secrets)
@modal.concurrent(max_inputs=20)
@modal.asgi_app()
def fastapi_app():
    os.environ.setdefault("SBX_BACKEND", "modal")
    return create_app()


@app.function(image=CONTROL_IMAGE, schedule=modal.Cron("*/5 * * * *"), secrets=_secrets)
def reap_cron() -> None:
    os.environ.setdefault("SBX_BACKEND", "modal")
    web = create_app()
    plane = web.state.plane
    reap(plane.store, plane.backend, datetime.now(UTC), idle_timeout_s=plane.idle_timeout_s)
