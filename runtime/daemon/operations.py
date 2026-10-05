"""Operation handlers — the daemon's mutating surface (RFC 167 §03).

Every handler takes a validated OperationEnvelope + returns a result dict
or raises OperationError(wire_code). Mutations run under the exclusive
worktree barrier when they can change captured content.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Any

from protocol.errors import (
    WIRE_CAPABILITY_UNSUPPORTED,
    WIRE_FENCE_STALE,
    WIRE_GRANT_EXPIRED,
    WIRE_PAYLOAD_INVALID,
    WIRE_PRECONDITION_FAILED,
    WIRE_RESOURCE_BUSY,
    WireError,
)
from protocol.manifests import FileEntry, FilesystemManifest, NativeContextBinding
from protocol.runtime import OperationEnvelope, request_digest

from runtime.harnesses.protocol import (
    ContextMismatch,
    CredentialBundle,
    TurnContext,
)
from runtime.harnesses.registry import get_harness
from runtime.security.paths import resolve

from .journal import Journal
from .supervisor import SupervisedProcess, Supervisor

READ_MAX_BYTES = 1 * 1024 * 1024
WRITE_MAX_BYTES = 4 * 1024 * 1024


class OperationError(Exception):
    def __init__(self, code: str, message: str, retry_advice: str = "none") -> None:
        super().__init__(message)
        self.error = WireError(code=code, message=message, retry_advice=retry_advice)


class WorktreeBarrier:
    """Exclusive mediated-mutation barrier (RFC 167 §03).

    At most one exclusive operation (turn/capture/checkpoint/apply/quiesce)
    holds the barrier. ``files.write`` is denied while an exclusive barrier
    is held by another operation.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder: str | None = None
        self._kind: str | None = None

    def acquire(self, operation_id: str, kind: str) -> None:
        with self._lock:
            if self._holder is None:
                self._holder = operation_id
                self._kind = kind
                return
            if self._holder == operation_id:
                return
            raise OperationError(
                WIRE_RESOURCE_BUSY,
                f"worktree barrier held by {self._holder} ({self._kind})",
                retry_advice="retry_later",
            )

    def release(self, operation_id: str) -> None:
        with self._lock:
            if self._holder == operation_id:
                self._holder = None
                self._kind = None

    def check_writes_allowed(self, operation_id: str | None) -> None:
        with self._lock:
            if self._holder is None or self._holder == operation_id:
                return
            raise OperationError(
                WIRE_RESOURCE_BUSY,
                "exclusive worktree barrier held; mediated writes denied",
                retry_advice="retry_later",
            )

    def holder(self) -> str | None:
        return self._holder


class Operations:
    """Dispatches OperationEnvelopes to daemon facilities."""

    def __init__(
        self,
        *,
        journal: Journal,
        supervisor: Supervisor,
        worktree_root: Path,
        state_root: Path,
        lease_id: str,
        lease_generation: int,
        grant_expires_at: float | None,
        emit: Any,  # callable(frame_dict) for operation.result pushes
    ) -> None:
        self.journal = journal
        self.supervisor = supervisor
        self.worktree_root = Path(worktree_root)
        self.state_root = Path(state_root)
        self.lease_id = lease_id
        self.lease_generation = lease_generation
        self.grant_expires_at = grant_expires_at
        self.barrier = WorktreeBarrier()
        self._emit = emit
        self._prepared: dict[str, Any] = {}  # session_id -> PreparedHarness
        self._shutdown = threading.Event()

    # -- envelope -----------------------------------------------------

    def _check_fences(self, env: OperationEnvelope) -> None:
        if env.lease_id != self.lease_id:
            raise OperationError(WIRE_FENCE_STALE, "lease id mismatch")
        if env.lease_generation != self.lease_generation:
            raise OperationError(WIRE_FENCE_STALE, "lease generation stale")
        if env.grant_expires_at is not None and env.grant_expires_at < time.time():
            raise OperationError(WIRE_GRANT_EXPIRED, "grant expired")

    def accept(self, env: OperationEnvelope) -> tuple[str, dict | None]:
        """Validate fences then fsync durable acceptance (or replay/conflict)."""
        self._check_fences(env)
        return self.journal.accept_operation(
            operation_id=env.operation_id,
            kind=env.operation_kind.value,
            session_id=env.session_id,
            request_digest=env.request_digest,
            envelope=env.to_dict(),
        )

    def run(self, env: OperationEnvelope) -> dict:
        """Run an accepted operation. Turn ops are async — they return a
        'starting' marker here; terminal arrives via operation.result."""
        self.journal.update_operation(env.operation_id, "starting")
        handler = getattr(self, "_op_" + env.operation_kind.value.replace(".", "_"), None)
        if handler is None:
            raise OperationError(
                WIRE_CAPABILITY_UNSUPPORTED, f"unknown operation {env.operation_kind}"
            )
        return handler(env)

    def settle_terminal(self, operation_id: str, state: str, result: dict) -> None:
        self.journal.update_operation(operation_id, state, result)
        self._emit(
            {
                "frame": "operation.result",
                "operation_id": operation_id,
                "state": state,
                "result": result,
                "final_local_seq": self.journal.spool_max_seq(),
            }
        )

    # -- turns ---------------------------------------------------------

    def _turn_context(self, env: OperationEnvelope) -> TurnContext:
        p = env.payload
        required = ("turn_id", "execution_id", "prompt")
        for key in required:
            if not p.get(key):
                raise OperationError(WIRE_PAYLOAD_INVALID, f"missing payload field {key}")
        binding = None
        raw_binding = p.get("native_binding")
        if raw_binding:
            binding = NativeContextBinding.from_dict(raw_binding)
        return TurnContext(
            session_id=env.session_id,
            turn_id=str(p["turn_id"]),
            execution_id=str(p["execution_id"]),
            attempt_ordinal=int(p.get("attempt_ordinal") or 1),
            effect_id=env.operation_id,
            lease_generation=env.lease_generation,
            worktree_root=self.worktree_root,
            worktree_generation=int(p.get("worktree_generation") or 0),
            prompt=str(p["prompt"]),
            model=p.get("model"),
            effort=p.get("effort"),
            instructions=p.get("instructions"),
            instructions_digest=p.get("instructions_digest"),
            attachments=tuple(p.get("attachments") or ()),
            result_contract=p.get("result_contract"),
            deadline=p.get("deadline"),
            native_binding=binding,
        )

    def _credentials(self, env: OperationEnvelope) -> CredentialBundle:
        cred = env.payload.get("credentials") or {}
        files = cred.get("files") or {}
        cenv = cred.get("env") or {}
        if not isinstance(files, dict) or not isinstance(cenv, dict):
            raise OperationError(WIRE_PAYLOAD_INVALID, "credentials shape")
        return CredentialBundle(
            files={str(k): str(v) for k, v in files.items()},
            env={str(k): str(v) for k, v in cenv.items()},
            lease_ref=cred.get("lease_ref"),
        )

    def _ensure_worktree(self, env: OperationEnvelope) -> dict:
        """Materialize the declared repository into the worktree boundary
        before the first turn (idempotent; skipped once .git exists)."""
        spec = env.payload.get("worktree") or {}
        if not spec:
            return {}
        git_cred = env.payload.get("git_credentials") or {}
        git_env = {str(k): str(v) for k, v in (git_cred.get("env") or {}).items()}

        def _event(kind: str, payload: dict) -> None:
            self.journal.spool_append(
                "observation",
                {
                    "kind": "diagnostic",
                    "type": kind,
                    "operation_id": env.operation_id,
                    "session_id": env.session_id,
                    "payload": payload,
                },
            )

        from . import worktree as _worktree

        try:
            return _worktree.ensure_worktree(self.worktree_root, spec, git_env, event=_event)
        except Exception as exc:
            raise OperationError(
                "worktree_bootstrap_failed", f"worktree materialization: {exc}"
            ) from exc

    def _op_turn_start(self, env: OperationEnvelope) -> dict:
        return self._start_or_resume(env, resume=False)

    def _op_turn_resume(self, env: OperationEnvelope) -> dict:
        return self._start_or_resume(env, resume=True)

    def _start_or_resume(self, env: OperationEnvelope, *, resume: bool) -> dict:
        provider = str(env.payload.get("provider_id") or "")
        if not provider:
            raise OperationError(WIRE_PAYLOAD_INVALID, "missing provider_id")
        context = self._turn_context(env)
        self._ensure_worktree(env)
        harness = get_harness(provider, self.state_root)
        prepared = self._prepared.get(env.session_id)
        if prepared is None:
            prepared = harness.prepare(context, self._credentials(env))
            self._prepared[env.session_id] = prepared
        if resume:
            if context.native_binding is None:
                raise OperationError(
                    WIRE_PRECONDITION_FAILED, "turn.resume requires native_binding"
                )
            try:
                invocation = harness.resume_turn(context, prepared, context.native_binding)
            except ContextMismatch as exc:
                raise OperationError("context_mismatch", str(exc)) from exc
        else:
            invocation = harness.start_turn(context, prepared)
        if context.native_binding is not None:
            envx = dict(invocation.env)
            envx["SBX_EXPECTED_NATIVE_ID"] = context.native_binding.native_id
            invocation = type(invocation)(
                argv=invocation.argv,
                cwd=invocation.cwd,
                env=envx,
                stdin=invocation.stdin,
                transport=invocation.transport,
                kind=invocation.kind,
            )
        self.barrier.acquire(env.operation_id, "turn")
        proc = SupervisedProcess(
            execution_id=context.execution_id,
            operation_id=env.operation_id,
            invocation=invocation,
            normalize=harness.normalize,
        )
        try:
            self.supervisor.launch(proc)
        except Exception:
            self.barrier.release(env.operation_id)
            raise
        self.journal.update_operation(env.operation_id, "started")
        return {"started": True, "pid": proc.popen.pid if proc.popen else None}

    def on_process_terminal(self, proc: SupervisedProcess, observations) -> None:
        """Supervisor terminal callback: classify outcome + settle durably."""
        provider_id = str(self._op_meta(proc.operation_id, "provider_id") or "opencode")
        try:
            harness = get_harness(provider_id, self.state_root)
        except KeyError:
            harness = None
        outcome = None
        if harness is not None:
            from runtime.harnesses.protocol import ProcessEvidence

            outcome = harness.classify_outcome(
                ProcessEvidence(
                    exit_code=proc.exit_code,
                    signal=proc.signal_number,
                    duration_ms=0,
                    stderr_tail=proc.stderr_tail,
                    cancel_requested=proc.cancel_requested,
                    observations=tuple(observations),
                )
            )
        state = "succeeded"
        if outcome is not None:
            from runtime.harnesses.protocol import OutcomeKind

            state = {
                OutcomeKind.SUCCESS: "succeeded",
                OutcomeKind.FAILURE: "failed",
                OutcomeKind.INTERRUPTED: "interrupted",
                OutcomeKind.UNKNOWN: "unknown",
            }[outcome.outcome]
        self.settle_terminal(
            proc.operation_id,
            state,
            {
                "outcome": outcome.to_dict() if outcome else {"outcome": "unknown"},
                "exit_code": proc.exit_code,
                "signal": proc.signal_number,
                "bad_frames": proc.bad_frames,
            },
        )
        self.barrier.release(proc.operation_id)

    def _op_meta(self, operation_id: str, key: str) -> Any:
        row = self.journal.get_operation(operation_id) or {}
        return (row.get("envelope") or {}).get("payload", {}).get(key)

    def _op_turn_status(self, env: OperationEnvelope) -> dict:
        target = str(env.payload.get("operation_id") or "")
        row = self.journal.get_operation(target)
        if row is None:
            raise OperationError(WIRE_PAYLOAD_INVALID, "unknown operation_id")
        return row

    def _op_turn_cancel(self, env: OperationEnvelope) -> dict:
        target = str(env.payload.get("operation_id") or "")
        found = self.supervisor.cancel(target)
        return {"cancel_signalled": found, "target": target}

    def _op_turn_steer(self, env: OperationEnvelope) -> dict:
        raise OperationError(WIRE_CAPABILITY_UNSUPPORTED, "steer unsupported by one-shot provider")

    # -- files / worktree ----------------------------------------------

    def _op_files_list(self, env: OperationEnvelope) -> dict:
        p = env.payload
        target = resolve(
            self.worktree_root, str(p.get("root") or "worktree"), str(p.get("path") or ".")
        )
        if not target.exists():
            raise OperationError(WIRE_PAYLOAD_INVALID, "path does not exist")
        if target.is_file():
            return {"entries": [self._file_entry(target)]}
        entries = []
        for child in sorted(target.iterdir())[:1000]:
            entries.append(self._file_entry(child))
        return {"entries": entries}

    def _file_entry(self, path: Path) -> dict:
        rel = path.resolve().relative_to(self.worktree_root.resolve()).as_posix()
        if path.is_dir():
            return {"path": rel, "kind": "dir"}
        if path.is_symlink():
            return {"path": rel, "kind": "symlink", "link_target": os.readlink(path)}
        return {"path": rel, "kind": "file", "size": path.stat().st_size}

    def _op_files_read(self, env: OperationEnvelope) -> dict:
        p = env.payload
        target = resolve(
            self.worktree_root, str(p.get("root") or "worktree"), str(p.get("path") or "")
        )
        if not target.is_file():
            raise OperationError(WIRE_PAYLOAD_INVALID, "not a file")
        size = target.stat().st_size
        if size > READ_MAX_BYTES:
            raise OperationError(WIRE_PAYLOAD_INVALID, "file exceeds read cap")
        data = target.read_bytes()
        import base64

        return {
            "path": str(p.get("path")),
            "size": size,
            "content_b64": base64.b64encode(data).decode(),
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        }

    def _op_files_write(self, env: OperationEnvelope) -> dict:
        p = env.payload
        self.barrier.check_writes_allowed(env.operation_id)
        path = str(p.get("path") or "")
        content_b64 = p.get("content_b64")
        expected_digest = p.get("digest")
        if content_b64 is None:
            raise OperationError(WIRE_PAYLOAD_INVALID, "missing content_b64")
        import base64

        data = base64.b64decode(content_b64)
        if len(data) > WRITE_MAX_BYTES:
            raise OperationError(WIRE_PAYLOAD_INVALID, "write exceeds cap")
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if expected_digest and expected_digest != digest:
            raise OperationError(WIRE_PRECONDITION_FAILED, "content digest mismatch")
        target = resolve(self.worktree_root, str(p.get("root") or "worktree"), path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {"path": path, "size": len(data), "digest": digest}

    def _op_files_upload(self, env: OperationEnvelope) -> dict:
        return self._op_files_write(env)

    # -- changes ---------------------------------------------------------

    def _op_changes_observe(self, env: OperationEnvelope) -> dict:
        return {"manifest": self._filesystem_manifest().to_dict()}

    def _op_changes_capture(self, env: OperationEnvelope) -> dict:
        self.barrier.acquire(env.operation_id, "capture")
        try:
            manifest = self._filesystem_manifest()
            self.journal.meta_set("last_capture", __import__("json").dumps(manifest.to_dict()))
            return {"manifest": manifest.to_dict(), "manifest_digest": manifest.digest()}
        finally:
            self.barrier.release(env.operation_id)

    def _filesystem_manifest(self) -> FilesystemManifest:
        entries: list[FileEntry] = []
        root = self.worktree_root
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if rel.startswith(".git"):
                continue
            if path.is_symlink():
                entries.append(FileEntry(path=rel, kind="symlink", link_target=os.readlink(path)))
            elif path.is_dir():
                entries.append(FileEntry(path=rel, kind="dir"))
            elif path.is_file():
                data = path.read_bytes()
                entries.append(
                    FileEntry(
                        path=rel,
                        kind="file",
                        size=len(data),
                        digest="sha256:" + hashlib.sha256(data).hexdigest(),
                    )
                )
        return FilesystemManifest(entries=tuple(entries), root="worktree")

    def _op_changes_apply(self, env: OperationEnvelope) -> dict:
        raise OperationError(WIRE_CAPABILITY_UNSUPPORTED, "changes.apply not implemented")

    def _op_changes_export(self, env: OperationEnvelope) -> dict:
        raise OperationError(WIRE_CAPABILITY_UNSUPPORTED, "changes.export not implemented")

    # -- misc -----------------------------------------------------------

    def _op_worktree_quiesce(self, env: OperationEnvelope) -> dict:
        self.barrier.acquire(env.operation_id, "quiesce")
        self.supervisor.quiesced.set()
        return {"quiesced": True}

    def _op_worktree_release(self, env: OperationEnvelope) -> dict:
        self.supervisor.quiesced.clear()
        self.barrier.release(env.operation_id)
        return {"quiesced": False}

    def _op_environment_prepare(self, env: OperationEnvelope) -> dict:
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        return {"ready": True}

    def _op_worktree_restore(self, env: OperationEnvelope) -> dict:
        raise OperationError(
            WIRE_CAPABILITY_UNSUPPORTED, "worktree.restore requires snapshot facility"
        )

    def _op_runtime_shutdown(self, env: OperationEnvelope) -> dict:
        self.supervisor.stop_all()
        self._shutdown.set()
        return {"shutdown": True}

    def _op_snapshot_prepare(self, env: OperationEnvelope) -> dict:
        raise OperationError(WIRE_CAPABILITY_UNSUPPORTED, "snapshot unsupported on this runtime")

    _op_snapshot_seal = _op_snapshot_prepare
    _op_snapshot_abort = _op_snapshot_prepare

    def _op_terminal_create(self, env: OperationEnvelope) -> dict:
        raise OperationError(WIRE_CAPABILITY_UNSUPPORTED, "terminal not implemented")

    _op_terminal_input = _op_terminal_create
    _op_terminal_close = _op_terminal_create

    def _op_service_ensure(self, env: OperationEnvelope) -> dict:
        raise OperationError(WIRE_CAPABILITY_UNSUPPORTED, "services not implemented")

    _op_service_stop = _op_service_ensure

    def _op_credential_writeback(self, env: OperationEnvelope) -> dict:
        raise OperationError(WIRE_CAPABILITY_UNSUPPORTED, "static keys never write back")


def digest_of_payload(payload: dict) -> str:
    return request_digest(payload)
