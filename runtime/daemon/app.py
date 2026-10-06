"""sbx-runtime daemon: authenticated, fenced, deduped operation frames over HTTP."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from protocol.runtime import (
    OPERATION_KINDS,
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    QUERY_KINDS,
    decode_grant,
    request_digest,
)

from runtime.daemon.journal import Journal, SpoolPressure
from runtime.daemon.services import Service
from runtime.daemon.supervisor import TurnRun, kill_group
from runtime.daemon.terminal import Terminal
from runtime.daemon.worktree import Worktree, WorktreeError, git
from runtime.harnesses.protocol import Harness, HarnessError, TurnContext
from runtime.security.artifacts import collect_known
from runtime.security.paths import PathEscape
from runtime.security.redaction import Redactor

MAX_BODY = 300 * 1024 * 1024
RUNTIME_BUILD = "sbx-runtime/1"


class Refused(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class RuntimeDaemon:
    def __init__(
        self,
        *,
        state_dir: Path,
        work_dir: Path,
        key: bytes,
        lease_id: str,
        generation: int,
        image_digest: str,
        harnesses: dict[str, Harness],
        lease_ttl: float = 1800.0,
        max_unacked: int = 20000,
    ) -> None:
        self.state_dir, self.work_dir = state_dir, work_dir
        self.key, self.lease_id, self.generation = key, lease_id, generation
        self.image_digest = image_digest
        self.harnesses = harnesses
        self.lease_ttl = lease_ttl
        self.authority_until = time.time() + lease_ttl
        self.journal = Journal(state_dir / "journal.sqlite", max_unacked=max_unacked)
        self.worktree = Worktree(work_dir, state_dir)
        self.runs: dict[str, TurnRun] = {}
        self.terminals: dict[str, Terminal] = {}
        self.services: dict[str, Service] = {}
        self.lock = threading.RLock()
        self.barrier: str | None = None
        self.writer_scopes: set[str] = set(json.loads(self.journal.meta("writer_scopes") or "[]"))
        self.pending_writers: set[str] = set()
        # Every credential value handed to this lease (memory only, never journaled):
        # the artifact policy refuses/redacts them in captures, checkpoints, reads, logs.
        self.known_secrets: set[str] = set()
        self.recovered_operations = self._recover()
        self._stop = threading.Event()
        threading.Thread(target=self._watchdog, daemon=True).start()

    # -- recovery ----------------------------------------------------------------------
    def _recover(self) -> list[str]:
        """Open operations from a previous incarnation are never relaunched.

        A recorded live pid is stopped/isolated; missing launch evidence is
        reported as ambiguous so control reconciles to unknown/interrupted.
        """
        recovered = []
        open_ops = self.journal.ops_open()
        for op in open_ops:
            if op["kind"] == "turn.start":
                # Fence old in-process supervisors before any recovery signals.
                self.journal.op_update(op["operation_id"], status="lost")
        # Launch scopes are durable before spawn, including successful services
        # and terminals whose process objects are unavailable after a restart.
        for scope in self.writer_scopes:
            if not kill_group(0, scope=scope):
                self.pending_writers.add(scope)
        for op in open_ops:
            recovered.append(op["operation_id"])
            if op["kind"] != "turn.start":
                self.journal.op_update(
                    op["operation_id"], status="lost", result={"reason": "runtime_restart"}
                )
                continue
            pid = op.get("pid")
            stopped = kill_group(pid or 0, scope=op["operation_id"])
            if not stopped:
                self.writer_scopes.add(op["operation_id"])
                self.pending_writers.add(op["operation_id"])
            launch = "started" if pid else "ambiguous"
            execution_id = (op.get("result") or {}).get("execution_id") or op["operation_id"]
            self.journal.append(
                execution_id,
                "execution.observed_terminal",
                {
                    "verdict": "unknown",
                    "error_code": "outcome_unknown",
                    "message": "runtime restarted during the Turn",
                    "launch": launch,
                    "credential_health": "unknown",
                    "retry_advice": "acknowledge",
                },
                terminal=True,
            )
            final = self.journal.last_seq() + 1
            self.journal.append(
                execution_id,
                "execution.stopped",
                {
                    "stopped": stopped,
                    "final_local_seq": final,
                    "cancelled": False,
                    "recovered": True,
                },
                terminal=True,
            )
            self.journal.op_update(
                op["operation_id"],
                status="lost",
                result={"verdict": "unknown", "launch": launch, "stopped": stopped},
            )
        self.journal.set_meta("writer_scopes", json.dumps(sorted(self.writer_scopes)))
        return recovered

    def _track_writer(self, scope: str) -> None:
        self.writer_scopes.add(scope)
        self.journal.set_meta("writer_scopes", json.dumps(sorted(self.writer_scopes)))

    def _require_recovered_quiet(self) -> None:
        for scope in list(self.pending_writers):
            if kill_group(0, scope=scope):
                self.pending_writers.remove(scope)
        if self.pending_writers:
            raise Refused("busy", "recovered writers have not confirmed stop")

    def _watchdog(self) -> None:
        while not self._stop.wait(5.0):
            if time.time() > self.authority_until:
                # Lease expiry stops mediated writes and terminates active processes.
                for run in list(self.runs.values()):
                    if not run.done.is_set() or run.stop_confirmed is False:
                        run.stop()

    # -- authorization -------------------------------------------------------------------
    def authorize(
        self, header: str | None, *, need: str, generation: int | None = None
    ) -> dict[str, Any]:
        if not header or not header.startswith("SBX-Grant "):
            raise Refused("unauthenticated", "missing runtime grant", 401)
        try:
            claims = decode_grant(self.key, header.split(" ", 1)[1])
        except ValueError as exc:
            raise Refused("unauthenticated", str(exc), 401) from exc
        if claims.get("lease_id") != self.lease_id:
            raise Refused("forbidden", "grant is for another lease", 403)
        if int(claims.get("generation", -1)) != self.generation or (
            generation is not None and generation != self.generation
        ):
            raise Refused("stale_fence", "lease generation is not current")
        if need == "manage" and claims.get("scope") != "manage":
            raise Refused("forbidden", "grant lacks manage scope", 403)
        self.authority_until = max(
            self.authority_until,
            min(float(claims["exp"]) + self.lease_ttl, time.time() + self.lease_ttl),
        )
        return claims

    # -- frames ----------------------------------------------------------------------------
    def hello(self, body: dict[str, Any]) -> dict[str, Any]:
        majors = body.get("protocol_majors") or [PROTOCOL_MAJOR]
        if PROTOCOL_MAJOR not in majors:
            raise Refused("runtime_incompatible", f"runtime speaks protocol major {PROTOCOL_MAJOR}")
        return {
            "lease_id": self.lease_id,
            "lease_generation": self.generation,
            "runtime_epoch": self.journal.epoch,
            "protocol": {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR},
            "image_digest": self.image_digest,
            "runtime_build": RUNTIME_BUILD,
            "harnesses": [h.describe().to_dict() for h in self.harnesses.values()],
            "recovered_operations": self.recovered_operations,
            "spool": {"last_local_seq": self.journal.last_seq(), "acked": self.journal.acked()},
            "health": self.health(),
        }

    def health(self) -> dict[str, Any]:
        active = [op for op, run in self.runs.items() if not run.done.is_set()]
        disk = shutil.disk_usage(self.state_dir)
        return {
            "active_operations": active,
            "unacked": self.journal.unacked(),
            "disk_free": disk.free,
            "authority_until": self.authority_until,
            "barrier": self.barrier,
        }

    def operation(self, frame: dict[str, Any]) -> dict[str, Any]:
        kind = frame.get("operation_kind")
        op_id = frame.get("operation_id")
        payload = frame.get("payload") or {}
        if kind not in OPERATION_KINDS or not op_id:
            raise Refused("validation_failed", "unknown operation", 422)
        if (
            frame.get("lease_id") != self.lease_id
            or int(frame.get("lease_generation", -1)) != self.generation
        ):
            raise Refused("stale_fence", "frame fence does not match this lease")
        digest = request_digest(kind, payload)
        if frame.get("request_digest") != digest:
            raise Refused("validation_failed", "request digest mismatch", 422)
        self.known_secrets |= collect_known(frame.get("secrets") or {})
        with self.lock:
            existing = self.journal.op_get(op_id)
            if existing:
                if existing["request_digest"] != digest or existing["kind"] != kind:
                    raise Refused("operation_conflict", "operation id reused with a different body")
                return {
                    "operation_id": op_id,
                    "status": existing["status"],
                    "result": existing["result"],
                    "replayed": True,
                }
            if kind == "turn.start":
                return self._turn_start(op_id, frame, payload)
            self.journal.op_insert(op_id, kind, digest, frame.get("session_id"), self.generation)
            try:
                result = self._sync_operation(kind, op_id, payload, frame.get("secrets") or {})
                self.journal.op_update(op_id, status="succeeded", result=result)
                return {"operation_id": op_id, "status": "succeeded", "result": result}
            except (WorktreeError, HarnessError, PathEscape, Refused) as exc:
                code = getattr(exc, "code", "validation_failed")
                message = Redactor(self.known_secrets).text(str(exc))[:500]
                result = {"error": {"code": code, "message": message}}
                self.journal.op_update(op_id, status="failed", result=result)
                return {"operation_id": op_id, "status": "failed", "result": result}

    def _active_run(self) -> TurnRun | None:
        for run in self.runs.values():
            if not run.done.is_set() or run.stop_confirmed is False:
                return run
        return None

    def _turn_start(
        self, op_id: str, frame: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_recovered_quiet()
        if self._active_run() is not None or self.barrier:
            raise Refused("busy", "another mutating CLI or barrier is active")
        if time.time() > self.authority_until:
            raise Refused("stale_fence", "lease authority expired")
        if self.journal.unacked() >= self.journal.max_unacked:
            raise Refused("spool_pressure", "evidence spool is full; ack before new work")
        provider = payload.get("provider_id")
        harness = self.harnesses.get(provider or "")
        if harness is None or harness.describe().support_tier == "disabled":
            raise Refused(
                "unsupported_capability", f"harness {provider} is not installed/enabled", 422
            )
        if not self.worktree.path.exists():
            raise Refused("executor_unavailable", "worktree not realized")
        for term in self.terminals.values():
            if not term.close():
                raise Refused("busy", "terminal writers have not confirmed stop")
        session_id = frame.get("session_id") or "session"
        context = TurnContext(
            session_id=session_id,
            turn_id=payload["turn_id"],
            execution_id=payload["execution_id"],
            operation_id=op_id,
            lease_generation=self.generation,
            worktree=self.worktree.path,
            home=self.state_dir / "homes" / session_id,
            prompt=payload["prompt"],
            model=payload.get("model"),
            effort=payload.get("effort"),
            native_binding=payload.get("native_binding"),
            deadline_seconds=float(payload.get("deadline_seconds") or 3600),
        )
        digest = request_digest("turn.start", payload)
        self.journal.op_insert(op_id, "turn.start", digest, session_id, self.generation)
        self._track_writer(op_id)
        self.journal.op_update(op_id, result={"execution_id": context.execution_id})
        try:
            self._place_native_state(session_id)
            prepared = harness.prepare(context, frame.get("secrets") or {})
            invocation = (
                harness.resume_turn(context, prepared, context.native_binding)
                if context.native_binding
                else harness.start_turn(context, prepared)
            )
        except HarnessError as exc:
            self.journal.append(
                context.execution_id,
                "execution.observed_terminal",
                {
                    "verdict": "failure",
                    "error_code": exc.code,
                    "message": Redactor(self.known_secrets).text(str(exc)),
                    "credential_health": "ok",
                    "retry_advice": "none",
                },
                terminal=True,
            )
            final = self.journal.last_seq() + 1
            self.journal.append(
                context.execution_id,
                "execution.stopped",
                {"stopped": True, "final_local_seq": final, "cancelled": False, "launched": False},
                terminal=True,
            )
            self.journal.op_update(
                op_id, status="failed", result={"verdict": "failure", "error_code": exc.code}
            )
            return {"operation_id": op_id, "status": "failed", "result": {"error_code": exc.code}}
        run = TurnRun(self.journal, op_id, harness, context, prepared, invocation)
        run.redactor = Redactor([*prepared.secrets, *self.known_secrets])
        self.runs[op_id] = run
        run.start()
        return {"operation_id": op_id, "status": "accepted", "result": None}

    def _place_native_state(self, session_id: str) -> None:
        staging = getattr(self.worktree, "_native_staging", None)
        if not staging:
            return
        source = Path(staging) / session_id
        if source.exists():
            target = self.state_dir / "homes" / session_id / ".local" / "share" / "opencode"
            codex_target = self.state_dir / "homes" / session_id / ".codex"
            target.mkdir(parents=True, exist_ok=True)
            for item in source.iterdir():
                dest = (codex_target if item.name == "sessions" else target) / item.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    continue
                shutil.move(str(item), str(dest))
        self.worktree._native_staging = None

    def _sync_operation(
        self, kind: str, op_id: str, payload: dict[str, Any], secrets: dict[str, Any]
    ) -> dict[str, Any]:
        if kind == "turn.cancel":
            target = payload.get("target_operation_id")
            run = self.runs.get(target or "")
            if run is None:
                op = self.journal.op_get(target or "")
                stopped = kill_group((op or {}).get("pid") or 0, scope=target) if op else True
                if stopped:
                    self.pending_writers.discard(target)
                return {"target_status": op["status"] if op else "unknown", "stopped": stopped}
            confirmed = run.stop()
            run.done.wait(timeout=15)
            return {"target_status": self.journal.op_get(target)["status"], "stopped": confirmed}
        if kind == "worktree.restore":
            self._require_quiet()
            return self.worktree.restore(payload, secrets.get("git"), self.known_secrets)
        if kind == "files.write":
            self._require_quiet()
            return self.worktree.write(
                payload["path"], payload["content"], payload.get("expected_digest")
            )
        if kind == "check.run":
            self._require_quiet()
            return self._run_check(payload)
        if kind == "snapshot.prepare":
            self._require_quiet()
            self._quiesce_services()
            self.barrier = op_id
            try:
                if self.journal.unacked() > 0 and payload.get("require_acked", True):
                    raise Refused(
                        "capture_failed", "unacknowledged evidence; flush spool before checkpoint"
                    )
                native = {}
                homes = self.state_dir / "homes"
                if homes.exists():
                    for home in homes.iterdir():
                        paths = [
                            p for h in self.harnesses.values() for p in h.native_state_paths(home)
                        ]
                        if paths:
                            native[home.name] = paths
                return self.worktree.checkpoint(native, self.known_secrets)
            finally:
                self.barrier = None
        if kind == "changes.capture":
            self._require_quiet()
            self._quiesce_services()
            from runtime.daemon.changes import capture

            self.barrier = op_id
            try:
                return capture(self.worktree, payload, self.known_secrets)
            finally:
                self.barrier = None
        if kind == "changes.apply":
            self._require_quiet()
            from runtime.daemon.changes import apply

            return apply(self.worktree, payload)
        if kind == "terminal.create":
            self._require_quiet()
            if not self.worktree.path.exists():
                raise Refused("executor_unavailable", "worktree not realized")
            self._track_writer(op_id)
            term = Terminal(self.worktree.path, self.state_dir / "terminal-home", scope=op_id)
            self.terminals[term.id] = term
            return {"terminal_id": term.id}
        if kind == "terminal.input":
            self._require_recovered_quiet()
            if self._active_run() is not None or self.barrier:
                raise Refused(
                    "busy", "terminal writer is revoked while a CLI Turn or barrier is active"
                )
            term = self.terminals.get(payload.get("terminal_id") or "")
            if term is None or term.closed:
                raise Refused("not_found", "terminal not found", 404)
            term.write(str(payload.get("data") or ""))
            self.worktree.bump()
            return {"accepted": True}
        if kind == "terminal.close":
            self._require_recovered_quiet()
            term_id = payload.get("terminal_id") or ""
            term = self.terminals.get(term_id)
            if term is not None:
                if not term.close():
                    raise Refused("busy", "terminal writers have not confirmed stop")
                self.terminals.pop(term_id)
            return {"closed": True}
        if kind == "service.ensure":
            self._require_recovered_quiet()
            if self.barrier:
                raise Refused("busy", "exclusive barrier is active")
            decl = payload.get("declaration") or {}
            if not decl.get("name") or not isinstance(decl.get("argv"), list):
                raise Refused(
                    "validation_failed", "service declaration with name and argv required", 422
                )
            if self._active_run() is not None and decl.get("exclusive"):
                raise Refused("busy", "exclusive service start refused during a Turn")
            svc = self.services.get(decl["name"])
            if svc is None or svc.decl != decl:
                if svc is not None and not svc.stop():
                    raise Refused("busy", "service writers have not confirmed stop")
                self._track_writer(op_id)
                svc = Service(
                    decl,
                    self.worktree.path,
                    self.state_dir / "service-home" / decl["name"],
                    known=self.known_secrets,
                    scope=op_id,
                )
                self.services[decl["name"]] = svc
            svc.start()
            time.sleep(0.2)
            return svc.status()
        if kind == "service.stop":
            self._require_recovered_quiet()
            svc = self.services.get(payload.get("name") or "")
            if svc is not None and not svc.stop():
                raise Refused("busy", "service writers have not confirmed stop")
            return {"name": payload.get("name"), "state": "stopped"}
        if kind == "lease.renew":
            self.authority_until = time.time() + float(payload.get("ttl_seconds") or self.lease_ttl)
            return {"authority_until": self.authority_until}
        if kind == "runtime.shutdown":
            for run in list(self.runs.values()):
                run.stop()
            return {"stopping": True}
        raise Refused("unsupported_capability", f"{kind} is not implemented by this runtime", 422)

    def _quiesce_services(self) -> None:
        """Services that may write captured roots are stopped; desired state lives in control."""
        for svc in self.services.values():
            if not svc.stop():
                raise Refused("busy", "service writers have not confirmed stop")
        for term in self.terminals.values():
            if not term.close():
                raise Refused("busy", "terminal writers have not confirmed stop")

    def _require_quiet(self) -> None:
        self._require_recovered_quiet()
        if self._active_run() is not None:
            raise Refused("busy", "a CLI Turn is active; exclusive operation refused")

    def _run_check(self, payload: dict[str, Any]) -> dict[str, Any]:
        argv = payload.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise Refused("validation_failed", "declared check argv required", 422)
        cwd = self.worktree.path
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=float(payload.get("timeout_seconds") or 600),
                env={
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HOME": str(self.state_dir / "check-home"),
                    "LANG": "C.UTF-8",
                    "CI": "1",
                },
            )
            code, out = proc.returncode, (proc.stdout + proc.stderr)
        except subprocess.TimeoutExpired:
            code, out = None, "check timed out"
        except OSError as exc:
            code, out = 127, f"cannot execute: {exc.strerror}"
        head = git(cwd, "rev-parse", "HEAD").stdout.strip()
        return {
            "name": payload.get("name"),
            "exit_code": code,
            "status": "passed" if code == 0 else ("unknown" if code is None else "failed"),
            "output_tail": Redactor(self.known_secrets).text(out[-6000:]),
            "head": head,
            "generation": self.worktree.generation,
        }

    def query(self, body: dict[str, Any]) -> dict[str, Any]:
        kind = body.get("kind")
        if kind not in QUERY_KINDS:
            raise Refused("validation_failed", "unknown query", 422)
        if kind == "operation.status":
            op = self.journal.op_get(body.get("operation_id") or "")
            if op is None:
                return {"status": "unknown"}
            return {"status": op["status"], "result": op["result"], "kind": op["kind"]}
        if kind == "health.report":
            return self.health()
        if kind == "changes.observe":
            return self.worktree.observe()
        if kind == "service.status":
            return {"items": [svc.status() for svc in self.services.values()]}
        if kind == "service.logs":
            svc = self.services.get(body.get("name") or "")
            if svc is None:
                raise Refused("not_found", "service not found", 404)
            return {
                "name": svc.decl["name"],
                "lines": list(svc.logs)[-int(body.get("limit") or 200) :],
            }
        if kind == "terminal.read":
            term = self.terminals.get(body.get("terminal_id") or "")
            if term is None:
                raise Refused("not_found", "terminal not found", 404)
            chunk = term.read(int(body.get("after") or 0))
            return {**chunk, "data": Redactor(self.known_secrets).text(chunk["data"])}
        if kind == "files.list":
            return {"items": self.worktree.list(body.get("path") or "")}
        if kind == "files.read":
            self.known_secrets |= collect_known(body.get("secrets") or {})
            return self.worktree.read(body["path"], self.known_secrets)
        raise Refused("unsupported_capability", f"{kind} not implemented", 422)

    def events(self, body: dict[str, Any]) -> dict[str, Any]:
        after = int(body.get("after") or 0)
        limit = max(1, min(int(body.get("limit") or 500), 2000))
        items = self.journal.after(after, limit)
        return {
            "runtime_epoch": self.journal.epoch,
            "items": items,
            "last_local_seq": self.journal.last_seq(),
            "acked": self.journal.acked(),
        }

    def ack(self, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("runtime_epoch") != self.journal.epoch:
            raise Refused("stale_fence", "ack for another runtime epoch")
        return {"acked": self.journal.ack(int(body.get("through") or 0))}

    # -- HTTP ----------------------------------------------------------------------------------
    def handle(
        self, method: str, path: str, headers: Any, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        try:
            if method == "GET" and path == "/healthz":
                return 200, {
                    "ok": True,
                    "protocol": {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR},
                }
            if method != "POST":
                raise Refused("not_found", "no such route", 404)
            auth = headers.get("Authorization")
            if path == "/rt/hello":
                self.authorize(auth, need="read")
                return 200, self.hello(body)
            if path == "/rt/op":
                self.authorize(
                    auth, need="manage", generation=int(body.get("lease_generation", -1))
                )
                return 200, self.operation(body)
            if path == "/rt/query":
                self.authorize(auth, need="read")
                return 200, self.query(body)
            if path == "/rt/events":
                self.authorize(auth, need="read")
                return 200, self.events(body)
            if path == "/rt/ack":
                self.authorize(auth, need="read")
                return 200, self.ack(body)
            raise Refused("not_found", "no such route", 404)
        except Refused as exc:
            message = Redactor(self.known_secrets).text(exc.message)
            return exc.status, {"error": {"code": exc.code, "message": message}}
        except (WorktreeError, PathEscape) as exc:
            code = getattr(exc, "code", "validation_failed")
            status = {"not_found": 404, "forbidden": 403}.get(code, 409)
            message = Redactor(self.known_secrets).text(str(exc))[:300]
            return status, {"error": {"code": code, "message": message}}
        except SpoolPressure as exc:
            return 409, {"error": {"code": "spool_pressure", "message": str(exc)}}

    def serve(self, host: str, port: int) -> ThreadingHTTPServer:
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _reply(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    self.send_error(413)
                    return
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = {}
                status, payload = daemon.handle(method, self.path, self.headers, body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                self._reply("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._reply("POST")

            def log_message(self, *args: Any) -> None:  # no request logs (may carry ids only)
                return

        server = ThreadingHTTPServer((host, port), Handler)
        server.daemon_threads = True
        return server
