"""Changes application: Worktree observation -> immutable ChangeSet; fenced apply (RFC 05)."""

from __future__ import annotations

import base64
from typing import Any

from protocol.manifests import canonical_manifest, content_digest, subject_digest

from control.application import access
from control.application.ports import (
    BlobStore,
    RuntimeConnector,
    RuntimeRefused,
    RuntimeUnavailable,
)
from control.domain.errors import DomainError
from control.domain.identity import Principal
from control.domain.ids import new_id
from control.jobs.model import Outcome, Retry, Succeeded
from control.security.redaction import redact_text

AUTO_CAPTURE_ROLES = ("developer", "integration", "coordinator")


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def changeset_view(cs: dict[str, Any], files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    view = {
        "id": cs["id"],
        "session_id": cs["session_id"],
        "source_turn_id": cs["source_turn_id"],
        "state": cs["state"],
        "origin": cs["origin"],
        "automatic_eligible": cs["automatic_eligible"],
        "manifest_version": cs["manifest_version"],
        "subject_digest": cs["subject_digest"],
        "repository": cs["repository"],
        "base_sha": cs["base_sha"],
        "baseline_tree": cs["baseline_tree"],
        "tree_sha": cs["tree_sha"],
        "head_sha": cs["head_sha"],
        "worktree_generation": cs["worktree_generation"],
        "file_count": cs["file_count"],
        "error": cs["error"],
        "created_at": _iso(cs["created_at"]),
        "sealed_at": _iso(cs["sealed_at"]),
    }
    if files is not None:
        view["files"] = [
            {"path": f["path"], "type": f["type"], "mode": f["mode"], "digest": f["content_digest"]}
            for f in files
        ]
    return view


class Changes:
    def __init__(self, tx: Any, connector: RuntimeConnector, blobs: BlobStore) -> None:
        self.tx = tx
        self.connector = connector
        self.blobs = blobs
        self.ready_hooks: list[Any] = []

    # ---------------------------------------------------------------- commands
    def request_capture(
        self,
        principal: Principal,
        session_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        origin = body.get("origin") or "explicit"
        if origin not in ("explicit", "salvage"):
            raise DomainError("validation_failed", "origin must be explicit or salvage")

        def fn(uow: Any) -> dict[str, Any]:
            session = access.owned(
                uow, principal, "sessions", session_id, lock=True, what="session"
            )
            replay = uow.dedupe_begin(
                principal_id=principal.user_id,
                workspace_id=session["workspace_id"],
                command_kind=f"changesets.capture:{session_id}",
                key=idempotency_key,
                request=body,
            )
            if replay is not None:
                return replay
            turn = None
            if body.get("source_turn_id"):
                turn = uow.get("turns", body["source_turn_id"])
                if turn is None or turn["session_id"] != session_id:
                    raise DomainError("not_found", "turn not found")
            if (
                turn is not None
                and turn["state"] in ("failed", "cancelled", "interrupted")
                and origin != "salvage"
            ):
                origin_eff = "salvage"
            else:
                origin_eff = origin
            cs = self.capture_in(uow, session, turn, origin_eff, actor=principal.user_id)
            response = {
                "changeset": changeset_view(cs),
                "event_watermark": uow.watermark(session_id),
            }
            uow.dedupe_finish(
                principal_id=principal.user_id,
                workspace_id=session["workspace_id"],
                command_kind=f"changesets.capture:{session_id}",
                key=idempotency_key,
                response=response,
            )
            return response

        return self.tx.run(fn)

    def capture_in(
        self,
        uow: Any,
        session: dict[str, Any],
        turn: dict[str, Any] | None,
        origin: str,
        *,
        actor: str,
    ) -> dict[str, Any]:
        worktree = uow.find_one("worktrees", {"session_id": session["id"]}, lock=True)
        if turn is not None:
            existing = uow.find_one(
                "changesets",
                {
                    "source_turn_id": turn["id"],
                    "worktree_generation": worktree["generation"],
                    "origin": origin,
                    "state": ["capturing", "ready"],
                },
            )
            if existing is not None:
                return existing
        cs = uow.insert(
            "changesets",
            {
                "id": new_id("changeset"),
                "workspace_id": session["workspace_id"],
                "session_id": session["id"],
                "source_turn_id": turn["id"] if turn else None,
                "worktree_id": worktree["id"],
                "worktree_generation": worktree["generation"],
                "origin": origin,
                "repository": worktree["repository"],
                "base_sha": worktree["base_sha"],
            },
        )
        uow.append_event(
            session,
            "changeset.capture_requested",
            {"changeset_id": cs["id"], "origin": origin, "generation": worktree["generation"]},
            actor=actor,
            changeset_id=cs["id"],
            turn_id=cs["source_turn_id"],
        )
        uow.enqueue_job(
            workspace_id=session["workspace_id"],
            kind="changeset.capture",
            target_id=cs["id"],
            session_id=session["id"],
        )
        return cs

    def on_turn_terminal(
        self, uow: Any, session: dict[str, Any], turn: dict[str, Any], state: str
    ) -> None:
        """Automatic capture after a successful Turn (subject for review/delivery)."""
        if state != "succeeded" or session["role"] not in AUTO_CAPTURE_ROLES:
            return
        if not (session["effective_spec"] or {}).get("repository"):
            return
        self.capture_in(uow, session, turn, "automatic", actor="application")

    # ------------------------------------------------------------------ jobs
    def handle_capture(self, ctx: Any) -> Outcome:
        cs_id = ctx.claim.job["changeset_id"]

        def read(uow: Any) -> tuple[Any, Any, Any, Any]:
            cs = uow.get("changesets", cs_id)
            lease = uow.find_one(
                "executor_leases", {"session_id": cs["session_id"], "state": "ready"}
            )
            turn = uow.get("turns", cs["source_turn_id"]) if cs["source_turn_id"] else None
            return cs, lease, turn, uow.get("worktrees", cs["worktree_id"])

        cs, lease, turn, worktree = ctx.db.read(read)
        if cs["state"] != "capturing":
            return Succeeded({"state": cs["state"]})
        if lease is None or worktree["availability"] != "live":
            ctx.commit(
                lambda uow: self._fail(
                    uow,
                    cs_id,
                    "executor_unavailable: activate the Session executor to capture live files",
                )
            )
            return Succeeded({"failed": "executor_unavailable"})
        payload = {"base_sha": worktree["base_sha"], "repository": worktree["repository"]}
        try:
            response = self.connector.channel(lease).op(
                "changes.capture", f"{cs_id}:capture", cs["session_id"], payload
            )
        except RuntimeUnavailable as exc:
            return Retry("executor_unavailable", str(exc)[:200])
        except RuntimeRefused as exc:
            if exc.code == "busy":
                return Retry("executor_busy", "a CLI Turn is active", retry_after=2)
            ctx.commit(lambda uow, e=exc: self._fail(uow, cs_id, f"{e.code}: {e}"))
            return Succeeded({"failed": exc.code})
        if response["status"] != "succeeded":
            error = (response.get("result") or {}).get("error") or {}
            ctx.commit(
                lambda uow: self._fail(uow, cs_id, f"{error.get('code')}: {error.get('message')}")
            )
            return Succeeded({"failed": error.get("code")})
        result = response["result"]
        problem = self._verify(cs, worktree, result)
        if problem:
            ctx.commit(lambda uow: self._fail(uow, cs_id, problem))
            return Succeeded({"failed": "integrity"})
        keys = self._store(cs, result)
        eligible = bool(
            cs["origin"] == "automatic"
            and turn is not None
            and turn["state"] == "succeeded"
            and turn["evidence_complete"]
        )
        ctx.commit(lambda uow: self._seal(uow, cs_id, result, keys, eligible))
        return Succeeded({"subject_digest": result["subject_digest"]})

    def _verify(
        self, cs: dict[str, Any], worktree: dict[str, Any], result: dict[str, Any]
    ) -> str | None:
        manifest = result["manifest"]
        rebuilt = canonical_manifest(
            repository=worktree["repository"],
            base_sha=worktree["base_sha"],
            baseline_tree=manifest["baseline_tree"],
            files=manifest["files"],
            tree_sha=manifest["tree_sha"],
        )
        if rebuilt != manifest or subject_digest(rebuilt) != result["subject_digest"]:
            return "manifest does not match its canonical digest or pinned baseline"
        patch = base64.b64decode(result["patch_b64"])
        if content_digest(patch) != result["patch_digest"]:
            return "patch integrity check failed"
        if redact_text(patch.decode("utf-8", "replace")) != patch.decode("utf-8", "replace"):
            return "secret guard: credential-looking content in captured patch"
        for entry in manifest["files"]:
            if entry["type"] == "deleted":
                continue
            data = result["blobs"].get(entry["digest"])
            if data is None or content_digest(base64.b64decode(data)) != entry["digest"]:
                return f"content blob missing or tampered for {entry['path']}"
        return None

    def _store(self, cs: dict[str, Any], result: dict[str, Any]) -> dict[str, str]:
        keys = {"patch": f"{cs['workspace_id']}/changesets/{cs['id']}/patch"}
        self.blobs.put(keys["patch"], base64.b64decode(result["patch_b64"]))
        for digest, data in result["blobs"].items():
            key = f"{cs['workspace_id']}/changesets/{cs['id']}/files/{digest.split(':')[1]}"
            self.blobs.put(key, base64.b64decode(data))
            keys[digest] = key
        return keys

    def _blob_row(
        self, uow: Any, cs: dict[str, Any], key: str, digest: str, size: int, cls: str
    ) -> str:
        blob = uow.insert(
            "blobs",
            {
                "id": new_id("blob"),
                "workspace_id": cs["workspace_id"],
                "storage_key": key,
                "digest": digest,
                "size_bytes": size,
                "content_class": cls,
                "state": "sealed",
                "sealed_at": uow.now(),
            },
        )
        uow.insert(
            "blob_references",
            {
                "id": new_id("blob"),
                "workspace_id": cs["workspace_id"],
                "blob_id": blob["id"],
                "entity_kind": "changeset",
                "entity_id": cs["id"],
            },
        )
        return blob["id"]

    def _seal(
        self, uow: Any, cs_id: str, result: dict[str, Any], keys: dict[str, str], eligible: bool
    ) -> None:
        cs = uow.get("changesets", cs_id, lock=True)
        if cs["state"] != "capturing":
            return
        manifest = result["manifest"]
        patch_len = len(base64.b64decode(result["patch_b64"]))
        patch_blob = self._blob_row(
            uow, cs, keys["patch"], result["patch_digest"], patch_len, "changeset_patch"
        )
        for entry in manifest["files"]:
            blob_id = None
            if entry["type"] != "deleted":
                blob_id = self._blob_row(
                    uow,
                    cs,
                    keys[entry["digest"]],
                    entry["digest"],
                    len(base64.b64decode(result["blobs"][entry["digest"]])),
                    "changeset_file",
                )
            uow.insert(
                "changeset_files",
                {
                    "changeset_id": cs_id,
                    "path": entry["path"],
                    "type": entry["type"],
                    "mode": entry["mode"],
                    "content_digest": entry["digest"],
                    "blob_id": blob_id,
                },
            )
        cs = uow.update(
            "changesets",
            cs_id,
            {
                "state": "ready",
                "manifest_version": manifest["manifest_version"],
                "subject_digest": result["subject_digest"],
                "baseline_tree": manifest["baseline_tree"],
                "tree_sha": manifest["tree_sha"],
                "patch_blob_id": patch_blob,
                "file_count": len(manifest["files"]),
                "automatic_eligible": eligible,
                "sealed_at": uow.now(),
            },
        )
        session = uow.get("sessions", cs["session_id"], lock=True)
        uow.append_event(
            session,
            "changeset.ready",
            {
                "changeset_id": cs_id,
                "subject_digest": cs["subject_digest"],
                "files": cs["file_count"],
                "automatic_eligible": eligible,
                "origin": cs["origin"],
            },
            actor="application",
            changeset_id=cs_id,
            turn_id=cs["source_turn_id"],
        )
        for hook in self.ready_hooks:
            hook(uow, session, cs)

    def _fail(self, uow: Any, cs_id: str, error: str) -> None:
        cs = uow.get("changesets", cs_id, lock=True)
        if cs["state"] != "capturing":
            return
        uow.update("changesets", cs_id, {"state": "failed", "error": error[:500]})
        session = uow.get("sessions", cs["session_id"], lock=True)
        uow.append_event(
            session,
            "changeset.capture_failed",
            {"changeset_id": cs_id, "error": error[:500]},
            actor="application",
            changeset_id=cs_id,
            turn_id=cs["source_turn_id"],
        )

    # ----------------------------------------------------------------- apply
    def request_apply(
        self,
        principal: Principal,
        changeset_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            cs = access.owned(uow, principal, "changesets", changeset_id, what="changeset")
            if cs["state"] != "ready":
                raise DomainError("stale_subject", "only sealed ChangeSets can be applied")
            dest = access.owned(
                uow,
                principal,
                "sessions",
                body.get("destination_session_id") or "",
                lock=True,
                what="session",
            )
            if dest["workspace_id"] != cs["workspace_id"]:
                raise DomainError("not_found", "session not found")
            worktree = uow.find_one("worktrees", {"session_id": dest["id"]}, lock=True)
            if uow.count("worktree_operations", {"worktree_id": worktree["id"], "state": "active"}):
                raise DomainError(
                    "version_conflict", "another Worktree barrier operation is active"
                )
            op = uow.insert(
                "worktree_operations",
                {
                    "id": new_id("worktree_operation"),
                    "workspace_id": dest["workspace_id"],
                    "worktree_id": worktree["id"],
                    "kind": "apply",
                    "expected_generation": body.get("expected_generation"),
                    "input": {"changeset_id": changeset_id, "subject_digest": cs["subject_digest"]},
                },
            )
            uow.append_event(
                dest,
                "worktree.apply_requested",
                {
                    "changeset_id": changeset_id,
                    "subject_digest": cs["subject_digest"],
                    "operation_id": op["id"],
                },
                actor=principal.user_id,
                changeset_id=changeset_id,
            )
            job = uow.enqueue_job(
                workspace_id=dest["workspace_id"],
                kind="changeset.apply",
                target_id=op["id"],
                session_id=dest["id"],
            )
            return {
                "operation_id": op["id"],
                "job_id": job,
                "event_watermark": uow.watermark(dest["id"]),
            }

        return self.tx.run(fn)

    def handle_apply(self, ctx: Any) -> Outcome:
        op_id = ctx.claim.job["worktree_operation_id"]

        def read(uow: Any) -> tuple[Any, Any, Any, Any]:
            op = uow.get("worktree_operations", op_id)
            worktree = uow.get("worktrees", op["worktree_id"])
            cs = uow.get("changesets", op["input"]["changeset_id"])
            lease = uow.find_one(
                "executor_leases", {"session_id": worktree["session_id"], "state": "ready"}
            )
            return op, worktree, cs, lease

        op, worktree, cs, lease = ctx.db.read(read)
        if op["state"] != "active":
            return Succeeded({"state": op["state"]})
        if lease is None:
            ctx.commit(lambda uow: self._finish_apply(uow, op_id, False, "executor_unavailable"))
            return Succeeded({"failed": "executor_unavailable"})
        patch = self.blobs.get(
            ctx.db.read(lambda uow: uow.get("blobs", cs["patch_blob_id"]))["storage_key"]
        )
        payload = {
            "changeset_id": cs["id"],
            "patch_b64": base64.b64encode(patch).decode(),
            "patch_digest": content_digest(patch),
            "baseline_tree": cs["baseline_tree"],
            "expected_generation": op["expected_generation"],
        }
        try:
            response = self.connector.channel(lease).op(
                "changes.apply", f"{op_id}:apply", worktree["session_id"], payload
            )
        except RuntimeUnavailable as exc:
            return Retry("executor_unavailable", str(exc)[:200])
        ok = response["status"] == "succeeded"
        reason = (
            None
            if ok
            else ((response.get("result") or {}).get("error") or {}).get("code", "apply_failed")
        )
        ctx.commit(lambda uow: self._finish_apply(uow, op_id, ok, reason, response.get("result")))
        return Succeeded({"applied": ok})

    def _finish_apply(
        self,
        uow: Any,
        op_id: str,
        ok: bool,
        reason: str | None,
        result: dict[str, Any] | None = None,
    ) -> None:
        op = uow.get("worktree_operations", op_id, lock=True)
        if op["state"] != "active":
            return
        uow.update(
            "worktree_operations",
            op_id,
            {"state": "completed" if ok else "aborted", "finished_at": uow.now()},
        )
        worktree = uow.get("worktrees", op["worktree_id"], lock=True)
        session = uow.get("sessions", worktree["session_id"], lock=True)
        if ok:
            uow.update(
                "worktrees",
                worktree["id"],
                {
                    "generation": int(
                        (result or {}).get("generation") or worktree["generation"] + 1
                    ),
                    "updated_at": uow.now(),
                },
            )
        uow.append_event(
            session,
            "worktree.applied",
            {
                "operation_id": op_id,
                "changeset_id": op["input"]["changeset_id"],
                "applied": ok,
                "reason": reason,
            },
            actor="application",
            changeset_id=op["input"]["changeset_id"],
        )

    # ---------------------------------------------------------------- queries
    def list(self, principal: Principal, session_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            access.owned(uow, principal, "sessions", session_id, what="session")
            return {
                "items": [
                    changeset_view(c)
                    for c in uow.find(
                        "changesets", {"session_id": session_id}, order="created_at DESC"
                    )
                ]
            }

        return self.tx.read(fn)

    def get(self, principal: Principal, changeset_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            cs = access.owned(uow, principal, "changesets", changeset_id, what="changeset")
            return changeset_view(
                cs, uow.find("changeset_files", {"changeset_id": changeset_id}, order="path")
            )

        return self.tx.read(fn)

    def diff(self, principal: Principal, changeset_id: str) -> dict[str, Any]:
        """Historical diff from the immutable manifest; never wakes compute."""
        cs, blob = self.tx.read(
            lambda uow: (
                lambda c: (c, uow.get("blobs", c["patch_blob_id"]) if c["patch_blob_id"] else None)
            )(access.owned(uow, principal, "changesets", changeset_id, what="changeset"))
        )
        if blob is None:
            raise DomainError("capture_failed", "ChangeSet has no sealed payload")
        data = self.blobs.get(blob["storage_key"])
        return {
            "changeset_id": changeset_id,
            "subject_digest": cs["subject_digest"],
            "diff": data[:2_000_000].decode("utf-8", "replace"),
            "truncated": len(data) > 2_000_000,
        }

    def file(self, principal: Principal, changeset_id: str, path: str) -> dict[str, Any]:
        def fn(uow: Any) -> tuple[Any, Any]:
            access.owned(uow, principal, "changesets", changeset_id, what="changeset")
            entry = uow.find_one("changeset_files", {"changeset_id": changeset_id, "path": path})
            if entry is None or entry["blob_id"] is None:
                raise DomainError("not_found", "file not in ChangeSet")
            return entry, uow.get("blobs", entry["blob_id"])

        entry, blob = self.tx.read(fn)
        data = self.blobs.get(blob["storage_key"])
        try:
            return {
                "path": path,
                "encoding": "utf-8",
                "content": data.decode("utf-8"),
                "digest": entry["content_digest"],
            }
        except UnicodeDecodeError:
            return {
                "path": path,
                "encoding": "base64",
                "content": base64.b64encode(data).decode(),
                "digest": entry["content_digest"],
            }

    def live(self, principal: Principal, session_id: str) -> dict[str, Any]:
        """Live observation from the current lease; reports executor_unavailable instead of waking."""
        lease = self.tx.read(
            lambda uow: (
                access.owned(uow, principal, "sessions", session_id, what="session"),
                uow.find_one("executor_leases", {"session_id": session_id, "state": "ready"}),
            )[1]
        )
        if lease is None:
            raise DomainError(
                "executor_unavailable",
                "no live executor; changes are available from captured ChangeSets",
                action="activate_executor",
            )
        try:
            return {
                "observation": self.connector.channel(lease).query("changes.observe"),
                "live": True,
            }
        except RuntimeUnavailable as exc:
            raise DomainError("executor_unavailable", "runtime unreachable") from exc
