"""ChangeSet capture, seal, read and apply (RFC 167 §05).

A ChangeSet is sealed immutable content derived from a quiesced Worktree.
Capture is a durable Job; a pending or failed capture never fabricates a
ready ChangeSet row. Seal happens only after generation/barrier checks and
content verification — the canonical ``subject_digest`` is SHA-256 over a
versioned manifest (sorted paths, no timestamps, no remote URLs).
"""

from __future__ import annotations

import base64
import hashlib

from protocol.runtime import OperationEnvelope, OperationKind

from control.domain import ids
from control.domain.changes import (
    CaptureOrigin,
    ChangeSetFile,
    automatic_eligible,
    canonical_manifest,
    subject_digest,
)
from control.domain.errors import DomainError, NotFound
from control.domain.jobs import JobKind, TargetFamily
from control.persistence.unit_of_work import SqlUnitOfWork

from .events import append_event
from .sessions import enqueue_job, enqueue_outbox

#: Runtime/credential/generated roots excluded from every capture. The
#: manifest comes from the daemon's worktree root, but the policy is
#: enforced application-side as a second line.
CAPTURE_EXCLUDE_PREFIXES = (
    ".git/",
    ".sbx/",
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
)

#: Never capture credential-bearing or secret-looking paths.
CAPTURE_EXCLUDE_NAMES = (
    "auth.json",
    ".modal.toml",
    ".env",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
)

OP_TIMEOUT_S = 120.0


class ChangeSetService:
    def __init__(
        self,
        db,
        blob_store,
        *,
        runtime_stack=None,
        execution_service=None,
    ) -> None:
        self.db = db
        self.blobs = blob_store
        self.stack = runtime_stack
        self.exec_service = execution_service or (
            getattr(runtime_stack, "service", None) if runtime_stack else None
        )

    # ------------------------------------------------------------- commands
    def request_capture(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        session_id: str,
        source_turn_id: str | None = None,
        origin: str = "explicit",
        baseline: dict | None = None,
    ) -> dict:
        """Enqueue a durable capture intent. Dedupes on the sealed-subject
        identity (session, source turn, worktree generation)."""
        session = uow.sessions.get(workspace_id, session_id)
        if session is None:
            raise NotFound("session")
        worktree = uow.worktrees.get_by_session(workspace_id, session_id)
        if worktree is None:
            raise NotFound("worktree")
        try:
            CaptureOrigin(origin)
        except ValueError:
            raise DomainError("validation_failed", f"bad capture origin {origin!r}")
        if source_turn_id:
            existing = uow.changesets.get_by_capture(
                workspace_id, session_id, source_turn_id, worktree["generation"]
            )
            if existing:
                return {"changeset_id": existing["id"], "deduplicated": True}
        job = enqueue_job(
            uow,
            workspace_id=workspace_id,
            kind=JobKind.CHANGESET_CAPTURE,
            target_family=TargetFamily.CHANGESET,
            target_id=session_id,
            dedupe_key=(
                f"changeset.capture:{session_id}:{source_turn_id or 'ad-hoc'}"
                f":{worktree['generation']}"
            ),
            payload={
                "session_id": session_id,
                "source_turn_id": source_turn_id,
                "origin": origin,
                "worktree_generation": worktree["generation"],
                "baseline": baseline or {},
            },
        )
        return {"job_id": job["id"], "worktree_generation": worktree["generation"]}

    def get(self, uow: SqlUnitOfWork, *, workspace_id: str, changeset_id: str) -> dict:
        cs = uow.changesets.get(workspace_id, changeset_id)
        if cs is None:
            raise NotFound("changeset")
        cs = dict(cs)
        cs["files"] = uow.changeset_files.list_for(changeset_id)
        return cs

    def list_for_session(
        self, uow: SqlUnitOfWork, *, workspace_id: str, session_id: str
    ) -> list[dict]:
        return uow.changesets.list_by_session(workspace_id, session_id)

    # ------------------------------------------------------------- capture job
    def perform_capture(self, job: dict, ctx) -> dict:
        """Job handler: quiesce → enumerate manifest → fetch blobs → seal.

        Everything below ``worktree_generation`` is pinned; sealing checks
        the pinned generation is still the live one (a later turn invalidates
        the capture and must re-request)."""
        if self.stack is None or self.exec_service is None:
            return {"skipped": "runtime plane not wired"}
        payload = job.get("payload") or {}
        workspace_id = job["workspace_id"]
        session_id = payload["session_id"]
        generation = int(payload["worktree_generation"])
        origin = payload.get("origin") or "explicit"
        source_turn_id = payload.get("source_turn_id")
        baseline = payload.get("baseline") or {}

        with SqlUnitOfWork(self.db, actor={"kind": "job", "id": job["id"]}) as uow:
            session = uow.sessions.get(workspace_id, session_id)
            worktree = uow.worktrees.get_by_session(workspace_id, session_id)
            if session is None or worktree is None:
                uow.commit()
                return {"skipped": "session gone"}
            if worktree["generation"] != generation:
                uow.commit()
                # Stale capture intent — a new turn advanced the worktree.
                return {
                    "skipped": "worktree generation advanced",
                    "generation": worktree["generation"],
                }
            if source_turn_id:
                dup = uow.changesets.get_by_capture(
                    workspace_id, session_id, source_turn_id, generation
                )
                if dup:
                    uow.commit()
                    return {"deduplicated": dup["id"]}
            # Exclusive capture barrier on the worktree.
            uow.conn.execute(
                "INSERT INTO worktree_operations (id, workspace_id, worktree_id,"
                " kind, operation_id, fence_generation, expected_generation, state)"
                " VALUES (%s,%s,%s,'capture',%s,%s,%s,'active')",
                (
                    ids.new_id("worktree_operation"),
                    workspace_id,
                    worktree["id"],
                    job["effect_id"],
                    generation,
                    generation,
                ),
            )
            uow.commit()
            try:
                result = self._capture_via_runtime(
                    workspace_id,
                    session,
                    worktree,
                    generation,
                    origin,
                    source_turn_id,
                    baseline,
                )
            finally:
                with SqlUnitOfWork(self.db, actor={"kind": "job", "id": job["id"]}) as u2:
                    u2.worktree_ops.complete(
                        workspace_id,
                        u2.rows.one(
                            "SELECT id FROM worktree_operations WHERE operation_id=%s",
                            (job["effect_id"],),
                        )["id"],
                        "completed",
                    )
                    u2.commit()
            return result

    def _capture_via_runtime(
        self,
        workspace_id,
        session,
        worktree,
        generation,
        origin,
        source_turn_id,
        baseline,
    ) -> dict:
        pool = self.stack.pool
        with SqlUnitOfWork(self.db, actor={"kind": "capture"}) as uow:
            lease = self.exec_service._ensure_lease(uow, session=session)
            uow.commit()
        if lease.get("_spawned") is None:
            with SqlUnitOfWork(self.db, actor={"kind": "capture"}) as uow:
                lease = self.exec_service._spawn_and_attach(uow, session=session, lease=lease)
                uow.commit()

        def submit(kind: OperationKind, payload: dict) -> dict:
            env = OperationEnvelope(
                operation_id=ids.new_id("effect"),
                operation_kind=kind,
                session_id=session["id"],
                lease_id=lease["id"],
                lease_generation=lease["generation"],
                payload=payload,
            )
            reply = pool.submit_for_result(lease["id"], env.to_dict(), timeout=OP_TIMEOUT_S)
            if reply.get("frame") == "operation.rejected":
                raise DomainError(
                    "executor_unavailable",
                    f"capture op rejected: {(reply.get('error') or {}).get('message')}",
                )
            if reply.get("state") != "succeeded":
                raise DomainError(
                    "executor_unavailable",
                    f"capture op ended {reply.get('state')}",
                    details={"result": (reply.get("result") or {}).get("outcome")},
                )
            return reply.get("result") or {}

        try:
            submit(OperationKind.WORKTREE_QUIESCE, {})
            observed = submit(OperationKind.CHANGES_OBSERVE, {})
            manifest = observed["manifest"]
            entries = []
            for ent in manifest.get("entries") or []:
                rel = ent["path"]
                if self._excluded(rel):
                    continue
                if ent.get("kind") != "file":
                    continue
                read = submit(
                    OperationKind.FILES_READ,
                    {"path": rel, "root": "worktree"},
                )
                data = base64.b64decode(read["content_b64"])
                if read.get("digest") != "sha256:" + hashlib.sha256(data).hexdigest():
                    raise DomainError("internal", f"read digest mismatch for {rel}")
                blob = self.blobs.put(workspace_id, data)
                entries.append(
                    {
                        "path": rel,
                        "file_type": "file",
                        "mode": 0o644,
                        "content_digest": blob["digest"],
                        "size": len(data),
                        "storage_key": blob["storage_key"],
                    }
                )
            return self._seal(
                workspace_id=workspace_id,
                session=session,
                worktree=worktree,
                generation=generation,
                origin=origin,
                source_turn_id=source_turn_id,
                baseline=baseline,
                entries=entries,
            )
        finally:
            try:
                submit(OperationKind.WORKTREE_RELEASE, {})
            except Exception:
                pass

    @staticmethod
    def _excluded(path: str) -> bool:
        base = path.rsplit("/", 1)[-1]
        if base in CAPTURE_EXCLUDE_NAMES:
            return True
        return any(path.startswith(p) for p in CAPTURE_EXCLUDE_PREFIXES)

    def _seal(
        self,
        *,
        workspace_id: str,
        session: dict,
        worktree: dict,
        generation: int,
        origin: str,
        source_turn_id: str | None,
        baseline: dict,
        entries: list[dict],
    ) -> dict:
        """Sealing is the only place a ChangeSet row appears; every entry
        was content-verified above and the generation must still pin."""
        with SqlUnitOfWork(self.db, actor={"kind": "capture"}) as uow:
            live = uow.worktrees.get(workspace_id, worktree["id"])
            if live is None or live["generation"] != generation:
                raise DomainError(
                    "version_conflict",
                    "worktree generation advanced during capture — capture aborted",
                )
            if source_turn_id:
                dup = uow.changesets.get_by_capture(
                    workspace_id, session["id"], source_turn_id, generation
                )
                if dup:
                    uow.commit()
                    return {"changeset_id": dup["id"], "deduplicated": True}
            files = [
                ChangeSetFile(
                    path=e["path"],
                    file_type=e["file_type"],
                    mode=int(e["mode"]),
                    content_digest=e["content_digest"],
                )
                for e in entries
            ]
            manifest = canonical_manifest(
                repository=worktree.get("repository"),
                projectless_namespace=(None if worktree.get("repository") else session["id"]),
                base_sha=baseline.get("base_sha") or worktree.get("base_sha"),
                baseline_digest=baseline.get("baseline_digest"),
                head_sha=baseline.get("head_sha"),
                tree_sha=baseline.get("tree_sha"),
                files=files,
            )
            digest = subject_digest(manifest)
            turn_succeeded = False
            if source_turn_id:
                t = uow.turns.get(workspace_id, source_turn_id)
                turn_succeeded = bool(t and t["state"] == "succeeded")
            cs_id = ids.new_id("changeset")
            uow.changesets.insert(
                {
                    "id": cs_id,
                    "workspace_id": workspace_id,
                    "session_id": session["id"],
                    "worktree_id": worktree["id"],
                    "worktree_generation": generation,
                    "subject_digest": digest,
                    "manifest_version": manifest["manifest_version"],
                    "source_turn_id": source_turn_id,
                    "repository": worktree.get("repository"),
                    "base_sha": manifest.get("base_sha"),
                    "head_sha": manifest.get("head_sha"),
                    "tree_sha": manifest.get("tree_sha"),
                    "capture_origin": origin,
                    "automatic_eligible": automatic_eligible(CaptureOrigin(origin), turn_succeeded),
                    "manifest": manifest,
                    "payload_refs": {
                        "blob_prefix": "worktree/",
                        "file_count": len(files),
                    },
                }
            )
            file_rows = []
            for e in entries:
                blob_id = ids.new_id("blob")
                uow.blobs.insert(
                    {
                        "id": blob_id,
                        "workspace_id": workspace_id,
                        "storage_key": e["storage_key"],
                        "digest": e["content_digest"],
                        "size": e["size"],
                        "class": "payload",
                        "state": "sealed",
                    }
                )
                file_rows.append(
                    {
                        "changeset_id": cs_id,
                        "path": e["path"],
                        "file_type": e["file_type"],
                        "mode": e["mode"],
                        "content_digest": e["content_digest"],
                        "blob_id": blob_id,
                    }
                )
            uow.changeset_files.bulk(file_rows)
            append_event(
                uow,
                workspace_id=workspace_id,
                session_id=session["id"],
                event_type="changeset.sealed",
                turn_id=source_turn_id,
                payload={
                    "changeset_id": cs_id,
                    "subject_digest": digest,
                    "file_count": len(files),
                    "origin": origin,
                },
            )
            enqueue_outbox(
                uow,
                workspace_id=workspace_id,
                destination="session",
                kind="changeset.sealed",
                dedupe_key=f"outbox.changeset.sealed:{cs_id}",
                payload={"changeset_id": cs_id, "subject_digest": digest},
                subscriber=session["id"],
            )
            uow.commit()
            return {"changeset_id": cs_id, "subject_digest": digest}

    # ------------------------------------------------------------- apply
    def apply(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        changeset_id: str,
        dest_session_id: str,
    ) -> dict:
        """Stage a sealed ChangeSet into a destination Worktree. Pins the
        destination generation and baseline; rejects conflicts without
        partial application (checked pre-write)."""
        cs = uow.changesets.get(workspace_id, changeset_id)
        if cs is None:
            raise NotFound("changeset")
        dest_wt = uow.worktrees.get_by_session(workspace_id, dest_session_id)
        dest_session = uow.sessions.get(workspace_id, dest_session_id)
        if dest_wt is None or dest_session is None:
            raise NotFound("session")
        if cs["repository"] and dest_wt.get("repository") not in (None, cs["repository"]):
            raise DomainError(
                "version_conflict",
                f"destination repository {dest_wt['repository']} != changeset {cs['repository']}",
            )
        if cs["base_sha"] and dest_wt.get("base_sha") not in (None, cs["base_sha"]):
            raise DomainError(
                "version_conflict",
                "destination base_sha does not match changeset baseline",
            )
        files = uow.changeset_files.list_for(changeset_id)
        blob_rows = {
            r["id"]: r
            for r in uow.rows.all(
                "SELECT * FROM blobs WHERE workspace_id=%s AND id = ANY(%s)",
                (workspace_id, [f["blob_id"] for f in files if f["blob_id"]]),
            )
        }
        payload = [
            {
                "path": f["path"],
                "file_type": f["file_type"],
                "mode": f["mode"],
                "content_digest": f["content_digest"],
                "symlink_target": f["symlink_target"],
                "content_b64": base64.b64encode(
                    self.blobs.read(blob_rows[f["blob_id"]]["storage_key"])
                ).decode()
                if f.get("blob_id") and f["blob_id"] in blob_rows
                else None,
            }
            for f in files
        ]
        # Staging intent is recorded before any runtime write; the actual
        # writes happen in the apply job under a barrier.
        op = ids.new_id("worktree_operation")
        uow.conn.execute(
            "INSERT INTO worktree_operations (id, workspace_id, worktree_id,"
            " kind, operation_id, fence_generation, expected_generation, state)"
            " VALUES (%s,%s,%s,'apply',%s,%s,%s,'active')",
            (op, workspace_id, dest_wt["id"], op, dest_wt["generation"], dest_wt["generation"]),
        )
        append_event(
            uow,
            workspace_id=workspace_id,
            session_id=dest_session_id,
            event_type="changeset.apply_requested",
            payload={
                "changeset_id": changeset_id,
                "dest_worktree_id": dest_wt["id"],
                "dest_generation": dest_wt["generation"],
            },
        )
        job = enqueue_job(
            uow,
            workspace_id=workspace_id,
            kind=JobKind.CHANGESET_APPLY,
            target_family=TargetFamily.CHANGESET,
            target_id=changeset_id,
            dedupe_key=f"changeset.apply:{changeset_id}:{dest_session_id}:{dest_wt['generation']}",
            payload={
                "changeset_id": changeset_id,
                "dest_session_id": dest_session_id,
                "worktree_operation_id": op,
                "files": payload,
            },
        )
        return {"job_id": job["id"], "worktree_operation_id": op}

    def perform_apply(self, job: dict, ctx) -> dict:
        """Job handler: write staged files into the destination worktree
        under the daemon barrier, then bump the destination generation."""
        if self.stack is None or self.exec_service is None:
            return {"skipped": "runtime plane not wired"}
        payload = job.get("payload") or {}
        workspace_id = job["workspace_id"]
        dest_session_id = payload["dest_session_id"]
        op_id = payload["worktree_operation_id"]
        files = payload.get("files") or []

        def finish(state: str, extra: dict | None = None) -> dict:
            with SqlUnitOfWork(self.db, actor={"kind": "job", "id": job["id"]}) as uow:
                uow.worktree_ops.complete(workspace_id, op_id, state)
                if state == "completed":
                    wt = uow.worktrees.get_by_session(workspace_id, dest_session_id)
                    if wt is not None:
                        uow.worktrees.update(
                            workspace_id,
                            wt["id"],
                            {"generation": wt["generation"] + 1},
                        )
                    append_event(
                        uow,
                        workspace_id=workspace_id,
                        session_id=dest_session_id,
                        event_type="changeset.applied",
                        payload={
                            "changeset_id": payload.get("changeset_id"),
                            "worktree_operation_id": op_id,
                            **(extra or {}),
                        },
                    )
                uow.commit()
            return {"state": state}

        with SqlUnitOfWork(self.db, actor={"kind": "job", "id": job["id"]}) as uow:
            session = uow.sessions.get(workspace_id, dest_session_id)
            if session is None:
                return finish("failed", {"reason": "session gone"})
            lease = self.exec_service._ensure_lease(uow, session=session)
            uow.commit()
        if lease.get("_spawned") is None:
            with SqlUnitOfWork(self.db, actor={"kind": "job", "id": job["id"]}) as uow:
                lease = self.exec_service._spawn_and_attach(uow, session=session, lease=lease)
                uow.commit()
        pool = self.stack.pool
        try:
            for f in files:
                if f["file_type"] != "file" or f.get("content_b64") is None:
                    continue
                env = OperationEnvelope(
                    operation_id=ids.new_id("effect"),
                    operation_kind=OperationKind.FILES_WRITE,
                    session_id=dest_session_id,
                    lease_id=lease["id"],
                    lease_generation=lease["generation"],
                    payload={
                        "path": f["path"],
                        "root": "worktree",
                        "content_b64": f["content_b64"],
                        "digest": f["content_digest"],
                    },
                )
                reply = pool.submit_for_result(lease["id"], env.to_dict(), timeout=OP_TIMEOUT_S)
                if reply.get("state") != "succeeded":
                    return finish("failed", {"reason": f"write failed: {f['path']}"})
        except Exception as exc:
            return finish("failed", {"reason": f"{type(exc).__name__}: {exc}"})
        return finish("completed", {"files": len(files)})
