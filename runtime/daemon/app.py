import os
import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from protocol.build import runtime_source_digest
from protocol.runtime import OperationFrame, ProtocolError

from runtime.daemon import changes, files, snapshots
from runtime.daemon.journal import Journal
from runtime.daemon.processes import Processes
from runtime.daemon.supervisor import Supervisor
from runtime.daemon.worktree import clone_auth
from runtime.harnesses.registry import catalog
from runtime.security.redaction import Redactor


class Runtime:
    def __init__(self, root, session_id, lease_id, generation, token, harness_factory=None):
        self.root = Path(root)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(self.root / "runtime/journal.sqlite")
        self.session_id, self.lease_id, self.generation, self.token = (
            session_id,
            lease_id,
            generation,
            token,
        )
        kwargs = {"harness_factory": harness_factory} if harness_factory else {}
        self.recovered = [
            row["id"]
            for row in self.journal.db.execute(
                "SELECT id FROM operations WHERE state!='terminal'"
            ).fetchall()
        ]
        self.supervisor = Supervisor(
            self.journal, self.worktree, self.root / "native-home", **kwargs
        )

        self.supervisor.known_secrets.update(Redactor([token]).values)
        self.processes = Processes(self)

    def hello(self):
        return {
            "protocol_major": 1,
            "protocol_minor": 0,
            "session_id": self.session_id,
            "lease_id": self.lease_id,
            "lease_generation": self.generation,
            "runtime_epoch": self.journal.epoch,
            "runtime_build_digest": runtime_source_digest(),
            "image_digest": os.environ.get("SBX_IMAGE_DIGEST", "local"),
            "harnesses": catalog(),
            "spool_watermark": self.journal.watermark,
            "worktree_generation": int(self.journal.metadata("generation")),
            "recovered_operation_ids": [
                operation
                for operation in self.recovered
                if self.journal.get(operation)["state"] != "terminal"
            ],
        }

    def authorize(self, frame):
        frame.validate_body()
        if (
            frame.session_id != self.session_id
            or frame.lease_id != self.lease_id
            or frame.lease_generation != self.generation
            or frame.grant_expires_at < time.time()
        ):
            raise ProtocolError("forbidden")

    def accept(self, frame):
        self.authorize(frame)
        with self.supervisor.lock:
            previous = self.journal.get(frame.operation_id)
            if (
                not previous
                and self.processes.has_writer()
                and frame.operation_kind not in {"terminal.close", "terminal.input", "turn.cancel"}
            ):
                raise ProtocolError("waiting_capacity")
            if (
                not previous
                and self.hello()["recovered_operation_ids"]
                and frame.operation_kind != "turn.cancel"
            ):
                raise ProtocolError("outcome_unknown")
            if (
                not previous
                and self.supervisor.active_operation
                and frame.operation_kind != "turn.cancel"
            ):
                raise ProtocolError("waiting_capacity")
            if self.journal.accept(frame):
                if frame.operation_kind in {"turn.start", "turn.resume"}:
                    self.supervisor.active_operation = frame.operation_id
                threading.Thread(target=self.perform, args=(frame,), daemon=True).start()
        return self.journal.get(frame.operation_id)

    def perform(self, frame):
        if frame.operation_kind in {"turn.start", "turn.resume"}:
            self.supervisor.run_turn(frame)
            return
        try:
            if frame.operation_kind == "turn.cancel":
                self.supervisor.stop(frame.payload["operation_id"])
                self.journal.update(frame.operation_id, "terminal", result={"stop_requested": True})
                return
            with self.supervisor.lock:
                if self.supervisor.active_operation:
                    raise ProtocolError("waiting_capacity")
                self.journal.update(frame.operation_id, "starting")
                payload = frame.payload
                if frame.operation_kind in {"terminal.open", "service.start"}:
                    result = self.processes.open(
                        payload, service=frame.operation_kind == "service.start"
                    )
                elif frame.operation_kind in {"terminal.close", "service.stop"}:
                    result = self.processes.close(
                        payload.get("terminal_id", payload.get("service_id"))
                    )
                    generation = int(self.journal.metadata("generation")) + 1
                    self.journal.metadata("generation", generation)
                    result["generation"] = generation
                elif frame.operation_kind == "terminal.input":
                    result = self.processes.write(payload["terminal_id"], payload["content"])
                elif frame.operation_kind == "changes.capture":
                    result = changes.capture(self, payload)
                elif frame.operation_kind == "changes.apply":
                    result = changes.apply(self, payload)
                elif frame.operation_kind == "snapshot.capture":
                    result = snapshots.capture(self)
                elif frame.operation_kind == "worktree.restore":
                    result = snapshots.restore(self, payload["manifest"])
                elif frame.operation_kind == "environment.prepare":
                    env = {
                        "PATH": "/usr/local/bin:/usr/bin:/bin",
                        "HOME": str(self.root / "setup-home"),
                        "LANG": "C.UTF-8",
                        "GIT_TERMINAL_PROMPT": "0",
                    }
                    for name, value in payload.get("env", {}).items():
                        if name in {"HOME", "PATH"} or name.startswith(
                            ("XDG_", "SBX_", "OPENCODE_")
                        ):
                            raise ProtocolError("forbidden")
                        env[name] = value
                    Path(env["HOME"]).mkdir(mode=0o700, exist_ok=True)
                    repo = payload.get("repository")
                    if repo:
                        if (
                            not repo.startswith("https://github.com/")
                            or "@" in repo[8:]
                            or "?" in repo
                        ):
                            raise ProtocolError("forbidden")
                        if not (self.worktree / ".git").exists():
                            clone_credential = payload.get("clone_credential", {})
                            self.supervisor.known_secrets.update(clone_credential.values())
                            with clone_auth(self.root, clone_credential, env) as clone_env:
                                subprocess.run(
                                    ["git", "clone", "--quiet", repo, str(self.worktree)],
                                    env=clone_env,
                                    check=True,
                                    capture_output=True,
                                    timeout=120,
                                )
                        base = payload["base_sha"]
                        subprocess.run(
                            ["git", "checkout", "--detach", base],
                            cwd=self.worktree,
                            env=env,
                            check=True,
                            capture_output=True,
                            timeout=30,
                        )
                    for argv in payload.get("setup", []):
                        subprocess.run(
                            argv,
                            cwd=self.worktree,
                            env=env,
                            check=True,
                            capture_output=True,
                            timeout=120,
                        )
                    result = {"ready": True, "generation": int(self.journal.metadata("generation"))}
                elif frame.operation_kind == "files.write":
                    if any(v in payload["content"] for v in self.supervisor.known_secrets):
                        raise ProtocolError("forbidden")
                    if payload["generation"] != int(self.journal.metadata("generation")):
                        raise ProtocolError("version_conflict")
                    result = files.write(
                        self.worktree, payload["path"], payload["content"], payload.get("digest")
                    )
                    self.journal.metadata("generation", payload["generation"] + 1)
                    result["generation"] = payload["generation"] + 1
                elif frame.operation_kind == "turn.cancel":
                    self.supervisor.stop(payload["operation_id"])
                    result = {"stop_requested": True}
                else:
                    raise ProtocolError("unsupported_capability")
                self.journal.update(frame.operation_id, "terminal", result=result)
        except BaseException as error:
            code = str(error) if isinstance(error, ProtocolError) else "runtime_failed"
            self.journal.update(frame.operation_id, "terminal", result={"error": code})


def create_runtime_app(runtime):
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        import hmac

        if not hmac.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + runtime.token
        ):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ProtocolError)
    async def error_handler(request, error):
        return JSONResponse({"error": str(error)}, status_code=409)

    @app.get("/hello")
    def hello():
        return runtime.hello()

    @app.post("/operations", status_code=202)
    def accept(frame: OperationFrame):
        return runtime.accept(frame)

    @app.get("/operations/{operation_id}")
    def status(operation_id: str):
        row = runtime.journal.get(operation_id)
        if not row:
            raise HTTPException(404)
        return row

    @app.get("/events")
    def events(after: int = 0):
        return {"runtime_epoch": runtime.journal.epoch, "events": runtime.journal.events(after)}

    @app.post("/events/ack")
    def ack(body: dict):
        runtime.journal.ack(body["local_seq"])
        return {"ack": body["local_seq"]}

    @app.post("/cancel/{operation_id}")
    def cancel(operation_id: str, frame: OperationFrame):
        if (
            frame.operation_kind != "turn.cancel"
            or frame.payload.get("operation_id") != operation_id
        ):
            raise ProtocolError("forbidden")
        return runtime.accept(frame)

    @app.api_route(
        "/services/{service_id}/proxy/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
    )
    async def proxy(service_id: str, path: str, request: Request):
        import httpx
        from fastapi.responses import Response

        item = runtime.processes.items.get(service_id)
        if (
            not item
            or not item["service"]
            or not item["port"]
            or item["process"].poll() is not None
        ):
            raise ProtocolError("executor_unavailable")
        if len(await request.body()) > 4_000_000:
            raise ProtocolError("quota_exhausted")
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            upstream = await client.request(
                request.method,
                "http://127.0.0.1:" + str(item["port"]) + "/" + path,
                params=request.query_params,
                content=await request.body(),
                headers={
                    "Content-Type": request.headers.get("content-type", "application/octet-stream")
                },
            )
        if len(upstream.content) > 4_000_000:
            raise ProtocolError("quota_exhausted")
        if any(value.encode() in upstream.content for value in runtime.supervisor.known_secrets):
            raise ProtocolError("forbidden")
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            headers={
                "Content-Type": upstream.headers.get("content-type", "application/octet-stream"),
                "Cache-Control": "no-store",
            },
        )

    @app.get("/processes/{process_id}")
    def process_status(process_id: str, after: int = 0):
        return runtime.processes.read(process_id, after)

    @app.get("/files")
    def file_list():
        return files.list_files(runtime.worktree)

    @app.get("/files/read")
    def file_read(path: str):
        return Redactor(runtime.supervisor.known_secrets).clean(files.read(runtime.worktree, path))

    return app
