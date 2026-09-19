"""Modal App entry: ASGI + reaper cron.

Tests import ``control.app`` only. This module imports ``modal`` and must not
be imported by unit/integration tests (no Modal connection).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import modal

from control.app import create_app
from control.config import (
    MODAL_APP_NAME,
    RUN_GRACE_S,
    app_secret_names,
    remote_env_overlay,
)
from control.reaper import reap
from control.service import release_lease_for_action

_APP_NAME = os.environ.get("SBX_MODAL_APP_NAME", MODAL_APP_NAME)
app = modal.App(_APP_NAME)

# Secret names resolve at deploy time so a parallel deployment (the bootstrap
# config's ``secrets.*`` values) mounts its own Secrets instead of sharing the
# contract defaults. The shared Codex Secret mounts only when ``codex`` is a
# selected provider (``SBX_PROVIDERS``) — an unselected provider's credential
# must never block a deploy (SOR-115).
_secrets = [modal.Secret.from_name(name) for name in app_secret_names()]

# SOR-138: the entrypoint mount only ships the function's own package
# (``control``). ``control.app`` transitively imports ``runtime.runner.*``
# (contract/run_store/api_v1) and ``control/backends/modal.py`` lazily
# imports ``runtime.image``, so ``runtime`` must be part of the image's
# source mount for a fresh deploy to start without a detached fixup.
CONTROL_IMAGE = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "fastapi",
        "httpx",
        "pydantic",
        "uvicorn",
        "anyio",
        "starlette",
    )
    .add_local_python_source("runtime")
)

# Deploy-time names/tunables the remote functions must see (dict/secret/image
# names, account seeding). ``remote_env_overlay`` is an allowlist — credential
# material only ever arrives through the Secret mounts above.
_REMOTE_ENV = remote_env_overlay(app_name=_APP_NAME)


@app.function(image=CONTROL_IMAGE, secrets=_secrets, env=_REMOTE_ENV)
@modal.concurrent(max_inputs=20)
@modal.asgi_app()
def fastapi_app():
    os.environ.setdefault("SBX_BACKEND", "modal")
    return create_app()


@app.function(
    image=CONTROL_IMAGE,
    schedule=modal.Cron("*/5 * * * *"),
    secrets=_secrets,
    env=_REMOTE_ENV,
)
def reap_cron() -> None:
    os.environ.setdefault("SBX_BACKEND", "modal")
    web = create_app()
    plane = web.state.plane
    v1_state = getattr(web.state, "v1_state", None)
    reap(
        plane.store,
        plane.backend,
        datetime.now(UTC),
        idle_timeout_s=plane.idle_timeout_s,
        run_grace_s=plane.turn_max_seconds + RUN_GRACE_S,
        # SOR-63: expired cooldowns return accounts to rotation; absent on
        # app.state until the registry is wired (P2-D bootstrap).
        account_registry=getattr(web.state, "account_registry", None),
        # SOR-80: timed_out / lost sessions must drop any held /v1 lease.
        on_action=lambda action: release_lease_for_action(v1_state, action),
    )
