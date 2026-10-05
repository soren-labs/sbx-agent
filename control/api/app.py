"""Single business API, injected resources, pure reads and durable command receipts."""

import asyncio
import json
from contextlib import asynccontextmanager
from uuid import uuid4

from control.api.schemas import (
    Apply,
    Capture,
    ConnectionCreate,
    ConnectionView,
    Continuation,
    CredentialReplace,
    DeliveryCreate,
    EmailRequest,
    FileWrite,
    Generation,
    Login,
    Merge,
    Message,
    PasswordChange,
    PasswordReset,
    ProjectCreate,
    ProjectPublish,
    Publish,
    SessionCreate,
    SessionPatch,
    SessionView,
    Spawn,
    TerminalOpen,
    TurnView,
    Version,
    Wait,
)
from control.application.access import owned
from control.domain.errors import DomainError, require
from control.runtime_client.ingress import RuntimeIngress
from fastapi import Depends, FastAPI, Header, Request, WebSocket
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse


def create_app(resources, *, run_worker=False, secure_cookies=True, console_dir=None):
    r = resources

    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()

        async def work():
            while not stop.is_set():
                worked = await asyncio.to_thread(r.worker.once)
                if getattr(r, "outbox_worker", None):
                    await asyncio.to_thread(r.outbox_worker.once)
                if not worked:
                    await asyncio.sleep(0.2)

        tasks = [asyncio.create_task(work()) for _ in range(4)] if run_worker else []
        yield
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="SBX unified business API", version="1.0.0", lifespan=lifespan)
    app.state.resources = r

    @app.middleware("http")
    async def enforce_origin(request, call_next):
        origin = request.headers.get("origin")
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.url.path.startswith("/api/")
            and origin
        ):
            expected = getattr(r, "control_origin", None) or str(request.base_url).rstrip("/")
            if origin.rstrip("/") != expected:
                return JSONResponse(
                    {
                        "error": {
                            "code": "forbidden",
                            "category": "authorization",
                            "message": "Origin rejected",
                            "retryable": False,
                            "details": {},
                            "request_id": uuid4().hex,
                        }
                    },
                    status_code=403,
                )
        return await call_next(request)

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        code = error.code
        status = (
            404
            if code == "not_found"
            else 403
            if code in {"forbidden", "credential_invalid"}
            else 429
            if code == "rate_limited"
            else 409
        )
        return JSONResponse(
            {
                "error": {
                    "code": code,
                    "category": "authorization" if status in {403, 404} else "conflict",
                    "message": code.replace("_", " "),
                    "retryable": code
                    in {"waiting_capacity", "rate_limited", "executor_unavailable"},
                    "details": {},
                    "request_id": uuid4().hex,
                }
            },
            status_code=status,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Pydantic's default error includes write-only input values. Never return it.
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_request",
                    "category": "validation",
                    "message": "Invalid request fields",
                    "retryable": False,
                    "details": {},
                    "request_id": uuid4().hex,
                }
            },
            status_code=422,
        )

    def principal(request: Request):
        authorization = request.headers.get("authorization", "")
        bearer = (
            authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None
        )
        p = r.identity.authenticate(request.cookies.get("sbx_login"), bearer)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            require("owner" in p.scopes, "forbidden")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not bearer:
            r.identity.verify_csrf(
                request.cookies.get("sbx_login"), request.headers.get("x-csrf-token")
            )
        return p

    def key(idempotency_key: str = Header(alias="Idempotency-Key")):
        require(0 < len(idempotency_key) <= 200, "invalid_request")
        return idempotency_key

    def body(model):
        return model.model_dump(exclude_unset=True)

    @app.get("/healthz")
    def health():
        return {"ok": True}

    @app.get("/readyz")
    def ready():
        with r.uow.transaction() as repo:
            repo.one("SELECT 1 AS ok")
        return {"ok": True}

    @app.post("/api/auth/register", status_code=201)
    def register(value: Login, k=Depends(key)):
        result = r.identity.register(value.email, value.password, key=k)
        return result

    @app.post("/api/auth/email-verifications")
    def verify(value: dict, k=Depends(key)):
        return r.identity.verify_email(value.get("verifier", ""))

    @app.post("/api/auth/password-reset-requests", status_code=202)
    def reset_request(value: EmailRequest, k=Depends(key)):
        return r.identity.request_reset(value.email, k)

    @app.post("/api/auth/password-resets")
    def password_reset(value: PasswordReset, k=Depends(key)):
        return r.identity.reset_password(value.verifier, value.password)

    @app.post("/api/auth/login")
    def login(value: Login, k=Depends(key)):
        cookie, csrf = r.identity.login(value.email, value.password, key=k)
        response = JSONResponse({"authenticated": True})
        response.set_cookie(
            "sbx_login",
            cookie,
            httponly=True,
            secure=secure_cookies,
            samesite="strict",
            max_age=604800,
        )
        response.set_cookie(
            "sbx_csrf",
            csrf,
            httponly=False,
            secure=secure_cookies,
            samesite="strict",
            max_age=604800,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/auth/logout")
    def logout(request: Request, p=Depends(principal), k=Depends(key)):
        r.identity.logout(request.cookies.get("sbx_login"))
        response = JSONResponse({"logged_out": True})
        response.delete_cookie("sbx_login")
        response.delete_cookie("sbx_csrf")
        return response

    @app.get("/api/me")
    def me(p=Depends(principal)):
        with r.uow.transaction() as repo:
            user = repo.one(
                "SELECT id,email,verified_at,identity_version FROM users WHERE id=%s", (p.user_id,)
            )
        return {**user, "workspace_ids": p.workspace_ids}

    @app.post("/api/auth/password-changes")
    def password_change(value: PasswordChange, p=Depends(principal), k=Depends(key)):
        return r.identity.password_change(p, value.current_password, value.password)

    @app.get("/api/api-keys")
    def keys(p=Depends(principal)):
        return r.identity.api_keys(p)

    @app.post("/api/api-keys", status_code=201)
    def issue_key(value: dict, p=Depends(principal), k=Depends(key)):
        return r.identity.issue_api_key(
            p, value.get("workspace_id"), value.get("scopes", ["read"]), k
        )

    @app.delete("/api/api-keys/{kid}")
    def revoke_key(kid: str, p=Depends(principal), k=Depends(key)):
        return r.identity.revoke_api_key(p, kid)

    @app.get("/api/workspaces")
    def workspaces(p=Depends(principal)):
        with r.uow.transaction() as repo:
            return {
                "items": repo.all(
                    "SELECT id,name FROM workspaces WHERE owner_id=%s ORDER BY id", (p.user_id,)
                )
            }

    @app.get("/api/workspaces/{wid}/projects")
    def projects(wid: str, limit: int = 50, cursor: str | None = None, p=Depends(principal)):
        return r.queries.list(p, wid, "projects", limit=limit, cursor=cursor)

    @app.get("/api/workspaces/{wid}/diagnostics")
    def diagnostics(wid: str, p=Depends(principal)):
        return r.queries.diagnostics(p, wid)

    @app.post("/api/workspaces/{wid}/projects", status_code=201)
    def create_project(wid: str, value: ProjectCreate, p=Depends(principal), k=Depends(key)):
        return r.projects.create(p, wid, body(value), k)

    @app.get("/api/projects/{pid}")
    def project(pid: str, p=Depends(principal)):
        return r.queries.detail(p, "projects", pid)

    @app.get("/api/projects/{pid}/versions")
    def versions(pid: str, p=Depends(principal)):
        project = r.queries.detail(p, "projects", pid)
        return r.queries.list(
            p, project["workspace_id"], "project_versions", filters={"project_id": pid}
        )

    @app.post("/api/projects/{pid}/versions", status_code=201)
    def publish(pid: str, value: ProjectPublish, p=Depends(principal), k=Depends(key)):
        return r.projects.publish(p, pid, body(value), k)

    @app.get("/api/workspaces/{wid}/connections")
    def connections(wid: str, limit: int = 50, cursor: str | None = None, p=Depends(principal)):
        return r.queries.list(p, wid, "connections", limit=limit, cursor=cursor)

    @app.post("/api/workspaces/{wid}/connections", status_code=202)
    def connect(wid: str, value: ConnectionCreate, p=Depends(principal), k=Depends(key)):
        return r.connections.create(p, wid, body(value), k)

    @app.get("/api/connections/{cid}", response_model=ConnectionView)
    def connection(cid: str, p=Depends(principal)):
        return r.connections.safe(p, cid)

    @app.post("/api/connections/{cid}/credential-versions", status_code=202)
    def replace(cid: str, value: CredentialReplace, p=Depends(principal), k=Depends(key)):
        return r.connections.replace(p, cid, body(value), k)

    @app.delete("/api/connections/{cid}")
    def disconnect(cid: str, value: Version, p=Depends(principal), k=Depends(key)):
        return r.connections.revoke(p, cid, value.expected_version, k)

    @app.post("/api/connections/{cid}/validations", status_code=202)
    def validate(cid: str, value: Version, p=Depends(principal), k=Depends(key)):
        return r.connections.validate(p, cid, value.expected_version, k)

    @app.get("/api/connections/{cid}/capabilities")
    def connection_catalog(cid: str, p=Depends(principal)):
        return r.connections.connection_catalog(p, cid)

    @app.get("/api/models")
    def models(connection_id: str, p=Depends(principal)):
        return r.connections.connection_catalog(p, connection_id)

    @app.get("/api/harnesses")
    def harnesses(p=Depends(principal)):
        from protocol.manifests import installed_catalog

        return {"items": installed_catalog()}

    @app.get("/api/executor-backends")
    def backends(p=Depends(principal)):
        return {
            "items": [
                {"id": "modal", "credential_kind": "modal"},
                *(
                    [{"id": "local", "credential_kind": None}]
                    if getattr(r, "allow_local", False)
                    else []
                ),
            ]
        }

    @app.get("/api/workspaces/{wid}/sessions")
    def sessions(wid: str, limit: int = 50, cursor: str | None = None, p=Depends(principal)):
        return r.queries.list(p, wid, "sessions", limit=limit, cursor=cursor)

    @app.post("/api/workspaces/{wid}/sessions", status_code=201)
    def create_session(wid: str, value: SessionCreate, p=Depends(principal), k=Depends(key)):
        payload = body(value)
        if payload.get("message") and payload.get("backend", "modal") == "modal":
            require(
                bool(
                    payload.get("modal_connection_id")
                    and payload.get("zen_connection_id")
                    and payload.get("model")
                ),
                "credential_invalid",
            )
        if payload.get("zen_connection_id") and payload.get("model"):
            catalog = r.connections.connection_catalog(p, payload["zen_connection_id"])
            require(
                any(m["id"] == payload["model"] for m in catalog["models"]),
                "unsupported_capability",
            )
        return r.sessions.create(p, wid, payload, k)

    @app.get("/api/sessions/{sid}", response_model=SessionView)
    def session(sid: str, p=Depends(principal)):
        return r.sessions.get(p, sid)

    @app.patch("/api/sessions/{sid}")
    def patch_session(sid: str, value: SessionPatch, p=Depends(principal), k=Depends(key)):
        return r.lifecycle.patch(p, sid, body(value), k)

    @app.post("/api/sessions/{sid}/messages", status_code=202)
    def send(sid: str, value: Message, p=Depends(principal), k=Depends(key)):
        return r.sessions.send(p, sid, body(value), k)

    @app.get("/api/turns/{tid}", response_model=TurnView)
    def turn(tid: str, p=Depends(principal)):
        return r.queries.detail(p, "turns", tid)

    @app.post("/api/turns/{tid}/cancellations", status_code=202)
    def cancel(tid: str, p=Depends(principal), k=Depends(key)):
        return r.sessions.cancel(p, tid, k)

    @app.post("/api/turns/{tid}/retries", status_code=202)
    def retry_turn(tid: str, p=Depends(principal), k=Depends(key)):
        return r.lifecycle.retry(p, tid, k)

    @app.post("/api/executions/{eid}/recovery-acknowledgements")
    def recovery(eid: str, p=Depends(principal), k=Depends(key)):
        return r.lifecycle.acknowledge(p, eid, k)

    @app.post("/api/sessions/{sid}/continuations", status_code=201)
    def continuation(sid: str, value: Continuation, p=Depends(principal), k=Depends(key)):
        return r.lifecycle.continuation(p, sid, body(value), k)

    @app.get("/api/sessions/{sid}/events")
    async def events(sid: str, request: Request, after: int = 0, p=Depends(principal)):
        page = r.queries.events(p, sid, after)
        if "text/event-stream" not in request.headers.get("accept", ""):
            return page

        async def stream():
            cursor = after
            for _ in range(300):
                if await request.is_disconnected():
                    break
                r.identity.authenticate(
                    request.cookies.get("sbx_login"),
                    request.headers.get("authorization", "").removeprefix("Bearer ") or None,
                )
                current = r.queries.events(p, sid, cursor)
                for event in current["events"]:
                    yield (
                        "id: "
                        + str(event["seq"])
                        + "\ndata: "
                        + json.dumps(jsonable_encoder(event))
                        + "\n\n"
                    )
                    cursor = event["seq"]
                yield ": keepalive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    def lifecycle_route(action):
        def endpoint(sid: str, value: Version, p=Depends(principal), k=Depends(key)):
            return r.lifecycle.change(p, sid, action, body(value), k)

        return endpoint

    for path, action in [
        ("archives", "archive"),
        ("unarchives", "unarchive"),
        ("closures", "close"),
    ]:
        app.post("/api/sessions/{sid}/" + path)(lifecycle_route(action))

    @app.get("/api/sessions/{sid}/executor")
    def executor(sid: str, p=Depends(principal)):
        with r.uow.transaction() as repo:
            owned(repo, "sessions", sid, p)
            lease = repo.one(
                "SELECT id,generation,backend,state,cleanup_confirmed FROM executor_le"
                "ases WHERE session_id=%s ORDER BY generation DESC LIMIT 1",
                (sid,),
            )
        return {"lease": lease}

    @app.post("/api/sessions/{sid}/executor/releases", status_code=202)
    def release(sid: str, p=Depends(principal), k=Depends(key)):
        return r.worktrees.release(p, sid, k)

    @app.post("/api/sessions/{sid}/snapshots", status_code=202)
    def checkpoint(sid: str, value: Generation, p=Depends(principal), k=Depends(key)):
        return r.worktrees.checkpoint(p, sid, value.generation, k)

    @app.get("/api/sessions/{sid}/files")
    def files(sid: str, path: str | None = None, p=Depends(principal)):
        return r.io.files(p, sid, path)

    @app.put("/api/sessions/{sid}/files", status_code=202)
    def write(sid: str, value: FileWrite, p=Depends(principal), k=Depends(key)):
        return r.io.operation(p, sid, "files.write", body(value), k)

    @app.post("/api/sessions/{sid}/terminals", status_code=202)
    def terminal(sid: str, value: TerminalOpen, p=Depends(principal), k=Depends(key)):
        return r.io.operation(p, sid, "terminal.open", body(value), k)

    @app.get("/api/sessions/{sid}/terminals/{oid}")
    def terminal_read(sid: str, oid: str, p=Depends(principal)):
        with r.uow.transaction() as repo:
            owned(repo, "sessions", sid, p)
            op = repo.one(
                "SELECT o.* FROM worktree_operations o JOIN worktrees w ON w.id=o.work"
                "tree_id WHERE o.id=%s AND w.session_id=%s",
                (oid, sid),
            )
            require(op is not None, "not_found")
        require(r.io.client(p, sid).hello["lease_id"] == op["lease_id"], "executor_unavailable")
        return r.io.client(p, sid).get("/processes/" + oid)

    @app.post("/api/sessions/{sid}/terminals/{oid}/closures", status_code=202)
    def terminal_close(sid: str, oid: str, p=Depends(principal), k=Depends(key)):
        return r.io.close_terminal(p, sid, oid, k)

    @app.post("/api/sessions/{sid}/terminals/{oid}/inputs", status_code=202)
    def terminal_input(sid: str, oid: str, value: dict, p=Depends(principal), k=Depends(key)):
        with r.uow.transaction() as repo:
            owned(repo, "sessions", sid, p)
            op = repo.one(
                "SELECT o.* FROM worktree_operations o JOIN worktrees w ON w.id=o.worktree_id "
                "WHERE o.id=%s AND w.session_id=%s AND o.kind='terminal.open' AND o.st"
                "ate='executing'",
                (oid, sid),
            )
            require(op is not None, "not_found")
        client = r.io.client(p, sid)
        require(client.hello["lease_id"] == op["lease_id"], "executor_unavailable")
        require(
            isinstance(value.get("content"), str) and len(value["content"]) <= 8192,
            "invalid_request",
        )
        client.submit(
            k, "terminal.input", {"terminal_id": oid, "content": value["content"]}, expires=30
        )
        return {"operation_id": k}

    @app.get("/api/sessions/{sid}/services")
    def services(sid: str, p=Depends(principal)):
        return r.services.list(p, sid)

    @app.post("/api/services/{service_id}/activations", status_code=202)
    def service_start(service_id: str, p=Depends(principal), k=Depends(key)):
        return r.services.action(p, service_id, "start", k)

    @app.post("/api/services/{service_id}/stops", status_code=202)
    def service_stop(service_id: str, p=Depends(principal), k=Depends(key)):
        return r.services.action(p, service_id, "stop", k)

    @app.get("/api/services/{service_id}/logs")
    def service_logs(service_id: str, p=Depends(principal)):
        with r.uow.transaction() as repo:
            service = owned(repo, "service_desires", service_id, p)
        return r.io.client(p, service["session_id"]).get("/processes/" + service_id)

    @app.post("/api/services/{service_id}/preview-grants")
    def service_preview(service_id: str, p=Depends(principal), k=Depends(key)):
        return r.services.preview(p, service_id)

    @app.get("/api/sessions/{sid}/changesets")
    def changesets(sid: str, p=Depends(principal)):
        row = r.sessions.get(p, sid)
        return r.queries.list(p, row["workspace_id"], "changesets", filters={"session_id": sid})

    @app.post("/api/sessions/{sid}/changesets", status_code=202)
    def capture(sid: str, value: Capture, p=Depends(principal), k=Depends(key)):
        return r.changes.capture(p, sid, body(value), k)

    @app.get("/api/changesets/{csid}")
    def changeset(csid: str, p=Depends(principal)):
        return r.changes.get(p, csid)

    @app.get("/api/changesets/{csid}/files")
    def captured_file(csid: str, path: str, p=Depends(principal)):
        cs = r.changes.get(p, csid)
        require(cs["state"] == "ready" and path in cs["manifest"]["blobs"], "not_found")
        from fastapi.responses import Response

        return Response(
            r.objects.get(cs["workspace_id"], cs["manifest"]["blobs"][path]),
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/blobs/{blob_id}")
    def blob(blob_id: str, p=Depends(principal)):
        with r.uow.transaction() as repo:
            item = owned(repo, "blobs", blob_id, p)
        from fastapi.responses import Response

        return Response(
            r.objects.get(item["workspace_id"], item["storage_key"]),
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/operations/{oid}")
    def operation(oid: str, p=Depends(principal)):
        with r.uow.transaction() as repo:
            item = repo.one(
                "SELECT o.* FROM worktree_operations o JOIN worktrees w ON w.id=o.work"
                "tree_id WHERE o.id=%s",
                (oid,),
            )
            require(item is not None and item["workspace_id"] in p.workspace_ids, "not_found")
            return {k: item[k] for k in ("id", "kind", "state", "result", "expected_generation")}

    @app.post("/api/changesets/{csid}/applications", status_code=202)
    def apply(csid: str, value: Apply, p=Depends(principal), k=Depends(key)):
        return r.io.apply(p, csid, body(value), k)

    @app.post("/api/changesets/{csid}/deliveries", status_code=202)
    def deliver(csid: str, value: DeliveryCreate, p=Depends(principal), k=Depends(key)):
        return r.deliveries.request(p, csid, body(value), k)

    @app.get("/api/deliveries/{did}")
    def delivery(did: str, p=Depends(principal)):
        return r.queries.detail(p, "deliveries", did)

    @app.post("/api/deliveries/{did}/retries", status_code=202)
    def retry_delivery(did: str, p=Depends(principal), k=Depends(key)):
        return r.deliveries.retry(p, did, k)

    @app.post("/api/deliveries/{did}/refreshes", status_code=202)
    def refresh(did: str, p=Depends(principal), k=Depends(key)):
        return r.deliveries.retry(p, did, k, reconcile=True)

    @app.post("/api/deliveries/{did}/merge-requests", status_code=202)
    def merge(did: str, value: Merge, p=Depends(principal), k=Depends(key)):
        return r.deliveries.merge(p, did, body(value), k)

    @app.post("/api/sessions/{sid}/delegations", status_code=202)
    def spawn(sid: str, value: Spawn, p=Depends(principal), k=Depends(key)):
        return r.delegations.spawn(p, sid, body(value), k)

    @app.get("/api/delegations/{did}")
    def delegation(did: str, p=Depends(principal)):
        return r.delegations.get(p, did)

    @app.post("/api/delegations/{did}/waits", status_code=202)
    def wait(did: str, value: Wait, p=Depends(principal), k=Depends(key)):
        return r.delegations.wait(p, did, k, value.seconds)

    @app.post("/api/delegations/{did}/result", status_code=202)
    def result(did: str, value: Publish, p=Depends(principal), k=Depends(key)):
        return r.delegations.publish(p, did, value.turn_id, k)

    @app.post("/api/delegations/{did}/cancellations", status_code=202)
    def cancel_child(did: str, p=Depends(principal), k=Depends(key)):
        return r.delegations.cancel(p, did, k)

    @app.get("/api/jobs/{jid}")
    def job(jid: str, p=Depends(principal)):
        return r.queries.detail(p, "jobs", jid)

    @app.post("/internal/tools", include_in_schema=False)
    def tool_call(value: dict, authorization: str = Header(default=""), k=Depends(key)):
        require(
            authorization.startswith("Bearer ") and getattr(r, "tool_grants", None), "forbidden"
        )
        require(
            isinstance(value.get("name"), str) and isinstance(value.get("arguments", {}), dict),
            "invalid_request",
        )
        return r.tool_grants.invoke(authorization[7:], value["name"], value.get("arguments", {}), k)

    ingress = RuntimeIngress(r.uow, r.master)

    @app.websocket("/internal/runtime/connect")
    async def runtime_connect(socket: WebSocket):
        await ingress.connect(socket)

    if console_dir:
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=console_dir, html=True), name="console")
    return app
