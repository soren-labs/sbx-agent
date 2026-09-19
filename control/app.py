"""Pure FastAPI session API. Tests import this module; Modal decorators live in modal_app.py."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from starlette._utils import create_collapsing_task_group
from starlette.types import Receive, Scope, Send

from control.api_v1 import router as api_v1_router
from control.backend import LocalProcessBackend, SandboxBackend
from control.config import (
    DEFAULT_MODEL,
    MAX_CONCURRENT,
    RUNS_DICT_NAME,
    SESSIONS_DICT_NAME,
    SSE_KEEPALIVE_S,
    WORKFLOWS_DICT_NAME,
    basic_credentials,
    default_runner_cmd,
    env_float,
    env_int,
    env_str,
    lifecycle_config,
)
from control.run_store import RunLedger, RunStore
from control.sandbox_io import sandbox_env
from control.service import (
    ConcurrencyLimit,
    ControlPlane,
    SessionConflict,
    format_sse,
    release_lease,
)
from control.store import InMemoryStore, SessionStore
from control.workflow_store import WorkflowStore

security = HTTPBasic(auto_error=False)


class CreateSessionRequest(BaseModel):
    title: str | None = None
    model: str | None = None


class PostMessageRequest(BaseModel):
    text: str = Field(min_length=1)


def _http_error(code: int, error: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(
        status_code=code,
        detail={"error": error, "code": code},
        headers=headers,
    )


class DisconnectAwareStreamingResponse(StreamingResponse):
    """Watch ``http.disconnect`` even on ASGI spec ≥ 2.4 (uvicorn).

    Starlette's ``StreamingResponse`` only listens for disconnect on older spec
    versions, so a ``tail -F`` generator would never run ``finally`` / ``kill``.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await super().__call__(scope, receive, send)
            return

        try:
            async with create_collapsing_task_group() as task_group:

                async def wrap(func: Callable[[], Awaitable[None]]) -> None:
                    try:
                        await func()
                    except OSError:
                        pass
                    except anyio.get_cancelled_exc_class():
                        pass
                    finally:
                        task_group.cancel_scope.cancel()

                task_group.start_soon(wrap, partial(self.stream_response, send))
                await wrap(partial(self.listen_for_disconnect, receive))
        except anyio.get_cancelled_exc_class():
            pass
        finally:
            with anyio.CancelScope(shield=True):
                aclose = getattr(self.body_iterator, "aclose", None)
                if callable(aclose):
                    await aclose()
            if self.background is not None:
                await self.background()


def _select_backend() -> SandboxBackend:
    kind = os.environ.get("SBX_BACKEND", "local")
    if kind == "modal":
        from control.backends.modal import ModalBackend

        return ModalBackend()
    return LocalProcessBackend()


def _select_store() -> SessionStore:
    kind = os.environ.get("SBX_BACKEND", "local")
    if kind == "modal":
        from control.store import ModalDictStore

        # ``SBX_SESSIONS_DICT`` lets a parallel deploy keep its own durable
        # Dict; the contract default is unchanged when unset.
        return ModalDictStore(env_str("SBX_SESSIONS_DICT", SESSIONS_DICT_NAME))
    return InMemoryStore()


def _select_run_store() -> RunStore:
    """SOR-82/A1: the durable run ledger's backing store.

    Production keeps run records in a ``modal.Dict`` (``sbx-runs``) so they
    survive control-plane restarts and sandbox teardown. Locally the ledger
    lives on disk under ``$SBX_RUN_STORE_DIR`` (or
    ``$XDG_STATE_HOME/sbx-browser/runs``) — same re-open semantics.
    """
    kind = os.environ.get("SBX_BACKEND", "local")
    if kind == "modal":
        from control.run_store import ModalDictRunStore

        return ModalDictRunStore(env_str("SBX_RUNS_DICT", RUNS_DICT_NAME))
    from pathlib import Path

    from control.run_store import FileRunStore

    override = os.environ.get("SBX_RUN_STORE_DIR")
    if override:
        return FileRunStore(override)
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return FileRunStore(base / "sbx-browser" / "runs")


def _xdg_state_dir(name: str) -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "sbx-browser" / name


def _select_artifact_store() -> Any:
    """SOR-83: durable artifact package store (survives sandbox teardown)."""
    if os.environ.get("SBX_BACKEND", "local") == "modal":
        from control.artifacts import ARTIFACTS_DICT_NAME, ModalDictArtifactStore

        return ModalDictArtifactStore(env_str("SBX_ARTIFACTS_DICT", ARTIFACTS_DICT_NAME))
    from control.artifacts import FileArtifactStore

    override = os.environ.get("SBX_ARTIFACT_STORE_DIR")
    return FileArtifactStore(override or _xdg_state_dir("artifacts"))


def _select_workspace_store() -> Any:
    """SOR-83: durable workspace record store."""
    if os.environ.get("SBX_BACKEND", "local") == "modal":
        from control.workspace import WORKSPACES_DICT_NAME, ModalDictWorkspaceStore

        return ModalDictWorkspaceStore(env_str("SBX_WORKSPACES_DICT", WORKSPACES_DICT_NAME))
    from control.workspace import FileWorkspaceStore

    override = os.environ.get("SBX_WORKSPACE_STORE_DIR")
    return FileWorkspaceStore(override or _xdg_state_dir("workspaces"))


def _select_environment_store() -> Any:
    """SOR-127: durable environment build-record store."""
    if os.environ.get("SBX_BACKEND", "local") == "modal":
        from control.config import ENVIRONMENTS_DICT_NAME
        from control.environment import ModalDictEnvironmentStore

        return ModalDictEnvironmentStore(env_str("SBX_ENVIRONMENTS_DICT", ENVIRONMENTS_DICT_NAME))
    from control.environment import FileEnvironmentStore

    override = os.environ.get("SBX_ENV_STORE_DIR")
    return FileEnvironmentStore(override or _xdg_state_dir("environments"))


def _select_snapshot_provider(backend: SandboxBackend) -> Any:
    """SOR-127: filesystem snapshot/restore seam for the environment cache.

    Modal uses the native ``Sandbox.snapshot_filesystem`` primitive (image
    ids as refs); local dev/tests get directory copies under the snapshot
    root. The worker sandbox never holds Modal control credentials — both
    directions are driven control-plane-side.
    """
    if os.environ.get("SBX_BACKEND", "local") == "modal":
        from control.backends.modal import ModalSnapshotProvider
        from control.config import ENV_SNAPSHOT_TIMEOUT_S, ENV_SNAPSHOT_TTL_S

        return ModalSnapshotProvider(
            backend,
            timeout_s=env_int("SBX_ENV_SNAPSHOT_TIMEOUT_S", ENV_SNAPSHOT_TIMEOUT_S),
            ttl_s=env_int("SBX_ENV_SNAPSHOT_TTL_S", ENV_SNAPSHOT_TTL_S),
        )
    from control.environment import LocalSnapshotProvider

    override = os.environ.get("SBX_ENV_SNAPSHOT_DIR")
    return LocalSnapshotProvider(backend, override or _xdg_state_dir("env-snapshots"))


def _select_workflow_store() -> WorkflowStore:
    """SOR-84 C1: the durable workflow/task metadata index.

    Production keeps the ``(owner, workflow_id) → tasks`` index in a
    ``modal.Dict`` (``sbx-workflows``) so it survives control-plane
    restarts. Locally it lives on disk under ``$SBX_WORKFLOW_STORE_DIR``
    (or ``$XDG_STATE_HOME/sbx-browser/workflows``) — same re-open
    semantics as the run ledger.
    """
    if os.environ.get("SBX_BACKEND", "local") == "modal":
        from control.workflow_store import ModalDictWorkflowStore

        return ModalDictWorkflowStore(env_str("SBX_WORKFLOWS_DICT", WORKFLOWS_DICT_NAME))
    from control.workflow_store import FileWorkflowStore

    override = os.environ.get("SBX_WORKFLOW_STORE_DIR")
    return FileWorkflowStore(override or _xdg_state_dir("workflows"))


def create_app(
    *,
    backend: SandboxBackend | None = None,
    store: SessionStore | None = None,
    run_store: RunStore | None = None,
    artifact_store: Any | None = None,
    workspace_store: Any | None = None,
    workflow_store: WorkflowStore | None = None,
    runner_cmd: list[str] | None = None,
    basic_user: str | None = None,
    basic_password: str | None = None,
    clock: Any | None = None,
    keepalive_s: float | None = None,
    max_concurrent: int | None = None,
    default_model: str | None = None,
    idle_timeout_s: int | None = None,
    turn_max_seconds: int | None = None,
) -> FastAPI:
    backend_kind = os.environ.get("SBX_BACKEND", "local")
    backend = backend or _select_backend()
    store = store or _select_store()
    run_store = run_store or _select_run_store()
    artifact_store = artifact_store or _select_artifact_store()
    workspace_store = workspace_store or _select_workspace_store()
    workflow_store = workflow_store or _select_workflow_store()
    runner_cmd = runner_cmd or default_runner_cmd(backend_kind=backend_kind)
    user_default, pass_default = basic_credentials()
    basic_user = basic_user if basic_user is not None else user_default
    basic_password = basic_password if basic_password is not None else pass_default
    keepalive = (
        keepalive_s
        if keepalive_s is not None
        else env_float("SBX_SSE_KEEPALIVE_SECONDS", SSE_KEEPALIVE_S)
    )
    from control.artifact_ops import HandoffStoreView, credential_forbidden_values
    from control.handoff import HandoffService
    from control.workspace import WorkspaceService

    # SOR-132/SOR-134 + SOR-135: one resolved lifecycle chain — the values
    # here are the same ones the reaper and ``Sandbox.create`` resolve
    # (``plane.idle_timeout_s`` is the post-session retention only; the
    # sandbox's native bound is ``lifecycle.sandbox_idle_timeout_s``).
    lifecycle = lifecycle_config()
    workspaces = WorkspaceService(backend, workspace_store, clock=clock)
    handoffs = HandoffService(workspaces, HandoffStoreView(artifact_store))
    plane = ControlPlane(
        backend,
        store,
        runner_cmd,
        clock=clock,
        max_concurrent=max_concurrent
        if max_concurrent is not None
        else env_int("SBX_MAX_CONCURRENT", MAX_CONCURRENT),
        default_model=default_model or os.environ.get("SBX_DEFAULT_MODEL", DEFAULT_MODEL),
        idle_timeout_s=idle_timeout_s if idle_timeout_s is not None else lifecycle.idle_timeout_s,
        turn_max_seconds=turn_max_seconds
        if turn_max_seconds is not None
        else lifecycle.turn_max_seconds,
        run_ledger=RunLedger(run_store, clock=clock),
        workspaces=workspaces,
        handoffs=handoffs,
    )

    app = FastAPI(title="sbx-control", version="0.1.1")
    app.include_router(api_v1_router)  # empty shell until P2-D (SOR-64)
    app.state.plane = plane
    app.state.run_store = run_store
    app.state.run_ledger = plane.run_ledger
    app.state.artifact_store = artifact_store
    app.state.workspace_store = workspace_store
    app.state.workspaces = workspaces
    app.state.handoffs = handoffs

    def _snapshot_on_close(rec: Any, handle: Any) -> None:
        """SOR-83: persist the declared workspace artifact before teardown.

        Best-effort — the close path swallows failures; agents without a
        declared workspace simply skip (``workspace_not_found``).
        """
        from control.artifact_ops import snapshot_workspace_artifact

        account_id = (rec.sandbox_tags or {}).get("account_id")
        registry = getattr(app.state, "account_registry", None)
        get_blob = getattr(registry, "get_credential_blob", None)
        blob = None
        if callable(get_blob) and account_id and account_id != "auto":
            try:
                blob = get_blob(account_id)
            except Exception:
                pass
        run_n = rec.turns or None
        snapshot_workspace_artifact(
            backend=backend,
            handle=handle,
            workspaces=workspaces,
            store=artifact_store,
            agent_id=rec.id,
            run_id=f"run-{run_n}" if run_n else None,
            forbidden_values=credential_forbidden_values(blob),
            ledger=plane.run_ledger,
            run_n=run_n,
        )

    plane.snapshot_hook = _snapshot_on_close

    # SOR-127 environment build/snapshot cache — opt-in (``SBX_ENV_CACHE=1``).
    # When armed, the /v1 worker resolves the workspace's last-known-good
    # build record before provisioning (restore instead of cold clone) and
    # fills the cache after a successful cold prepare. Snapshot/restore run
    # control-plane-side; build sandboxes carry no credentials.
    app.state.environments = None
    if os.environ.get("SBX_ENV_CACHE") == "1":
        from control.environment import EnvironmentService

        snapshot_provider = _select_snapshot_provider(backend)
        environments = EnvironmentService(
            backend,
            _select_environment_store(),
            snapshots=snapshot_provider,
            setup=os.environ.get("SBX_ENV_SETUP") or "",
            clock=clock,
        )
        plane.snapshot_provider = snapshot_provider
        plane.environments = environments
        app.state.environments = environments

    app.state.workflow_store = workflow_store
    # SOR-82 integration: the durable run ledger is the source of truth, and
    # the /v1 run-state seam (begin/get/list/transition) binds to it by
    # default. Tests may still inject a substitute on app.state.run_states or
    # app.state.v1_state.
    from control.api_v1.lifecycle import LedgerRunStates
    from control.api_v1.state import V1State

    app.state.run_states = LedgerRunStates(plane.run_ledger)
    app.state.v1_state = V1State(run_states=app.state.run_states)
    app.state.basic_user = basic_user
    app.state.basic_password = basic_password
    app.state.keepalive_s = keepalive

    # P2.1 real-gate wiring is opt-in via a Modal Secret. Local/tests without
    # SBX_V1_BOOTSTRAP_KEY keep the existing lazy in-memory /v1 defaults.
    from control.api_v1.bootstrap import configure_v1_bootstrap

    configure_v1_bootstrap(app)

    # SOR-147 (WP-H1): automatic OAuth credential write-back. The registry is
    # resolved lazily — bootstrap seeds ``app.state.account_registry`` above,
    # tests may install one later — so the sync is inert until a registry
    # exists. Modal deployments also refresh the managed ``<prefix><id>``
    # Secret in place (no redeploy); local writes stay in the account store.
    from control.credsync import CredentialSync, ModalCredentialSecretWriter

    plane.credential_sync = CredentialSync(
        lambda: getattr(app.state, "account_registry", None),
        secret_writer=(ModalCredentialSecretWriter() if backend_kind == "modal" else None),
    )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            return JSONResponse(
                status_code=exc.status_code,
                content=exc.detail,
                headers=exc.headers,
            )
        code = exc.status_code
        return JSONResponse(
            status_code=code,
            content={"error": str(exc.detail), "code": code},
            headers=exc.headers,
        )

    def require_basic(
        credentials: HTTPBasicCredentials | None = Depends(security),
    ) -> str:
        import secrets

        if credentials is None:
            raise _http_error(
                401,
                "unauthorized",
                headers={"WWW-Authenticate": "Basic"},
            )
        user_ok = secrets.compare_digest(credentials.username, app.state.basic_user)
        pass_ok = secrets.compare_digest(credentials.password, app.state.basic_password)
        if not (user_ok and pass_ok):
            raise _http_error(
                401,
                "unauthorized",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials.username

    @app.post("/api/sessions", status_code=201)
    def create_session(
        body: CreateSessionRequest | None = None,
        owner: str = Depends(require_basic),
    ) -> dict[str, str]:
        body = body or CreateSessionRequest()
        try:
            session_id = plane.create_session(owner=owner, title=body.title, model=body.model)
        except ConcurrencyLimit as exc:
            raise _http_error(exc.code, exc.error) from exc
        return {"session_id": session_id}

    @app.get("/api/sessions")
    def list_sessions(_: str = Depends(require_basic)) -> list[dict[str, Any]]:
        return plane.list_sessions()

    @app.get("/api/sessions/{sid}")
    def get_session(sid: str, _: str = Depends(require_basic)) -> dict[str, Any]:
        rec = plane.get(sid)
        if rec is None:
            raise _http_error(404, "not_found")
        return plane.public(rec)

    @app.post("/api/sessions/{sid}/messages", status_code=202)
    def post_message(
        sid: str,
        body: PostMessageRequest,
        _: str = Depends(require_basic),
    ) -> dict[str, str]:
        try:
            turn_id = plane.post_message(sid, body.text)
        except KeyError:
            raise _http_error(404, "not_found") from None
        except SessionConflict as exc:
            raise _http_error(exc.code, exc.error) from exc
        return {"turn_id": turn_id}

    @app.post("/api/sessions/{sid}/stop", status_code=202)
    def stop_session(sid: str, _: str = Depends(require_basic)) -> dict[str, str]:
        try:
            status_name = plane.stop(sid)
        except KeyError:
            raise _http_error(404, "not_found") from None
        return {"status": status_name}

    @app.delete("/api/sessions/{sid}")
    def delete_session(sid: str, _: str = Depends(require_basic)) -> dict[str, Any]:
        try:
            rec = plane.close(sid)
        except KeyError:
            raise _http_error(404, "not_found") from None
        # SOR-80: sessions may hold a /v1 scheduler lease even when closed
        # through the internal API — release it idempotently.
        release_lease(getattr(app.state, "v1_state", None), sid)
        return plane.public(rec)

    @app.get("/api/sessions/{sid}/events")
    async def session_events(
        sid: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        _: str = Depends(require_basic),
    ) -> DisconnectAwareStreamingResponse:
        rec = plane.get(sid)
        if rec is None:
            raise _http_error(404, "not_found")
        try:
            last_id = int(last_event_id) if last_event_id else 0
        except ValueError:
            last_id = 0
        start_line = max(1, last_id + 1)

        handle = rec.handle()
        poll = plane.backend.poll(handle) if handle is not None else None
        keepalive_s: float = app.state.keepalive_s

        async def gen() -> AsyncIterator[str]:
            proc: Any = None
            try:
                if handle is not None and poll is not None and poll.alive:
                    proc = await asyncio.to_thread(
                        plane.backend.exec,
                        handle,
                        ["tail", "-n", f"+{start_line}", "-F", str(handle.root / "events.jsonl")],
                        sandbox_env(handle),
                    )
                    line_q: queue.Queue[tuple[str, str | None]] = queue.Queue()

                    def _reader() -> None:
                        try:
                            for line in proc.stdout:
                                line_q.put(("line", line))
                        except Exception:
                            pass
                        finally:
                            line_q.put(("eof", None))

                    threading.Thread(target=_reader, daemon=True, name="sbx-sse-tail").start()
                else:
                    line_q = None

                yield ": keepalive\n\n"
                if proc is None or line_q is None:
                    while True:
                        await asyncio.sleep(keepalive_s)
                        yield ": keepalive\n\n"
                    return

                lineno = start_line
                next_ka = time.monotonic() + keepalive_s
                while True:
                    try:
                        kind, payload = line_q.get_nowait()
                    except queue.Empty:
                        now = time.monotonic()
                        if now >= next_ka:
                            yield ": keepalive\n\n"
                            next_ka = now + keepalive_s
                        await asyncio.sleep(0.05)
                        continue
                    if kind == "eof":
                        break
                    raw = payload or ""
                    current = lineno
                    lineno += 1
                    if not raw.strip():
                        continue
                    try:
                        obj = json.loads(raw)
                        if not isinstance(obj, dict):
                            obj = {"type": "error", "message": raw}
                    except json.JSONDecodeError:
                        obj = {"type": "error", "message": "bad json in event stream"}
                    yield format_sse(current, obj)
                    now = time.monotonic()
                    if now >= next_ka:
                        yield ": keepalive\n\n"
                        next_ka = now + keepalive_s
            finally:
                if proc is not None:
                    # Must not await: this finally often runs under a cancelled
                    # cancel-scope (client disconnect), which would abort kill().
                    try:
                        proc.kill()
                    except Exception:
                        pass

        return DisconnectAwareStreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


# Local ``uvicorn control.app:app``. Tests should call ``create_app(...)``.
app = create_app()


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="sbx-control local server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
