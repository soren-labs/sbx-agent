"""Modal App entry: ASGI + reaper cron.

Tests import ``control.app`` only. This module imports ``modal`` and must not
be imported by unit/integration tests (no Modal connection).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import modal

from control.app import create_app
from control.config import (
    MODAL_APP_NAME,
    app_secret_names,
    control_warmth_config,
    lifecycle_config,
    remote_env_overlay,
)
from control.reaper import ReapAction, reap
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
    # SOR-223: the Task API's repo probe falls back to ``git ls-remote`` for
    # non-github.com remotes (github.com goes through the REST API).
    .apt_install("git")
    .pip_install(
        "fastapi",
        "httpx",
        "pydantic",
        "uvicorn",
        "anyio",
        "starlette",
        # GitHub App JWT signing (control.github_app) needs PyJWT's crypto
        # extra for RS256 — same constraint as pyproject.toml.
        "pyjwt[crypto]>=2.10.0",
    )
    # SOR-211 + SOR-266: ship the V2 Session Console build (``console/dist``)
    # with the control plane — the deployed app's URL serves it at "/" on
    # the same origin as ``/v1``. The legacy ``web/`` static UI no longer
    # ships in the image at all. Copied into the image (not mounted) so the
    # deploy needs no runtime mount resolution; a missing ``console/dist``
    # fails the deploy loudly instead of falling back to the legacy UI.
    # Build steps must precede deferred ``add_local_*`` mounts — a
    # ``copy=True`` layer after ``add_local_python_source`` raises
    # InvalidError at deploy time (SOR-219 acceptance). Dev checkouts carry
    # node_modules / playwright output under console/ — never bake them in.
    .add_local_dir(
        Path(
            os.environ.get("SBX_CONSOLE_DIST")
            or Path(__file__).resolve().parents[1] / "console" / "dist"
        ).resolve(),
        remote_path="/root/console",
        copy=True,
        ignore=lambda p: (
            "node_modules" in p.parts or "test-results" in p.parts or "playwright-report" in p.parts
        ),
    )
    .add_local_python_source("runtime")
)

# Deploy-time names/tunables the remote functions must see (dict/secret/image
# names, account seeding). ``remote_env_overlay`` is an allowlist — credential
# material only ever arrives through the Secret mounts above.
_REMOTE_ENV = {
    **remote_env_overlay(app_name=_APP_NAME),
    # SOR-266: the React console build is the only product UI at "/"; the
    # env is explicit so a missing /root/console fails startup loudly
    # rather than silently falling back to the legacy web/ directory.
    "SBX_CONSOLE_DIR": "/root/console",
}

# SOR-203: web-function autoscaler warmth, resolved at deploy time from
# SBX_CONTROL_SCALEDOWN_WINDOW_S / SBX_CONTROL_MIN_CONTAINERS /
# SBX_CONTROL_BUFFER_CONTAINERS (defaults: 300s warm tail, no always-on
# containers). Deliberately *not* applied to ``reap_cron`` — a 5-minute
# cron's own startup latency is irrelevant and warming it would be pure
# cost. The Agent Sandbox lifecycle (``Sandbox.create`` timers) is
# untouched by these knobs.
_WARMTH = control_warmth_config()


@app.function(
    image=CONTROL_IMAGE,
    secrets=_secrets,
    env=_REMOTE_ENV,
    scaledown_window=_WARMTH.scaledown_window_s,
    min_containers=_WARMTH.min_containers or None,
    buffer_containers=_WARMTH.buffer_containers or None,
)
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

    def _on_action(action: ReapAction) -> None:
        # SOR-180: a suspended agent keeps its account lease — it is
        # recoverable and returns under the same Agent/account on the
        # next follow-up. ``platform_loss`` IS terminal (uncheckpointed
        # loss) — release + settle like ``lost``.
        if action.kind != "suspended":
            release_lease_for_action(v1_state, action)
        if action.kind in ("lost", "timed_out", "platform_loss") and action.session_id:
            # SOR-139: the session just went terminal — persist a terminal
            # verdict for any still-open run so no record dangles RUNNING.
            plane.settle_orphaned_runs(
                action.session_id,
                session_status="lost" if action.kind == "platform_loss" else action.kind,
            )

    # SOR-139: this plane owns no turn watchers, so every ``running`` record
    # is watcher-less — settle those with written turn evidence into
    # FINISHED + idle before the reaper judges staleness. The reconcile
    # phase must never starve the reaper: a throwing tick here killed every
    # sweep before it ran, which is how >10min zombies survived every cron
    # tick (SOR-271 round-3 — fix the invocation path, not just resilience).
    try:
        plane.reconcile_turns()
    except Exception:
        pass
    # SOR-132/SOR-134: the reaper's bounds resolve from the same lifecycle
    # chain as the plane and ``Sandbox.create`` — including the graces,
    # which are env-tunable (``SBX_CREATE_GRACE_S`` / ``SBX_RUN_GRACE_S``).
    lifecycle = lifecycle_config()
    reap(
        plane.store,
        plane.backend,
        datetime.now(UTC),
        idle_timeout_s=lifecycle.idle_timeout_s,
        create_grace_s=lifecycle.create_grace_s,
        run_grace_s=lifecycle.run_stale_s,
        # SOR-63: expired cooldowns return accounts to rotation; absent on
        # app.state until the registry is wired (P2-D bootstrap).
        account_registry=getattr(web.state, "account_registry", None),
        # SOR-180: idle-expired agents checkpoint + release to suspended
        # (recoverable); idle agents lost before checkpointing surface as
        # explicit platform_loss.
        checkpoints=getattr(plane, "checkpoints", None),
        # SOR-80: timed_out / lost sessions must drop any held /v1 lease.
        on_action=_on_action,
    )
