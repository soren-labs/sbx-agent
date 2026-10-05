"""Row → resource serializers for the unified /api.

Safe views only: credential payloads, vault material, job internals, lease
handles and remote tokens never leave through these shapes.
"""

from __future__ import annotations

from typing import Any


def _ts(row: dict, key: str) -> str | None:
    v = row.get(key)
    return v.isoformat() if hasattr(v, "isoformat") else v


def user_view(u: dict, memberships: list[dict]) -> dict:
    return {
        "id": u["id"],
        "email": u.get("email_normalized"),
        "display_name": u.get("display_name"),
        "verified": u.get("verified_at") is not None,
        "workspaces": [m["id"] for m in memberships],
        "created_at": _ts(u, "created_at"),
    }


def api_key_view(k: dict) -> dict:
    return {
        "id": k["id"],
        "label": k.get("label"),
        "scopes": k.get("scopes") or [],
        "expires_at": _ts(k, "expires_at"),
        "created_at": _ts(k, "created_at"),
        "revoked": k.get("revoked_at") is not None,
    }


def workspace_view(w: dict, role: str | None = None) -> dict:
    return {
        "id": w["id"],
        "name": w.get("name"),
        "owner_user_id": w.get("owner_user_id"),
        "role": role,
        "created_at": _ts(w, "created_at"),
    }


def project_view(p: dict) -> dict:
    return {
        "id": p["id"],
        "workspace_id": p["workspace_id"],
        "slug": p["slug"],
        "name": p["name"],
        "current_version_id": p.get("current_version_id"),
        "metadata": p.get("metadata") or {},
        "version": p.get("version"),
        "created_at": _ts(p, "created_at"),
    }


def project_version_view(v: dict) -> dict:
    return {
        "id": v["id"],
        "project_id": v["project_id"],
        "ordinal": v["ordinal"],
        "repository": v.get("repository"),
        "base_ref": v.get("base_ref"),
        "environment": v.get("environment") or {},
        "services": v.get("services") or [],
        "defaults": v.get("defaults") or {},
        "ship_policy": v.get("ship_policy") or {},
        "spec_digest": v.get("spec_digest"),
        "created_at": _ts(v, "created_at"),
    }


def connection_view(c: dict) -> dict:
    return {
        "id": c["id"],
        "workspace_id": c["workspace_id"],
        "kind": c["kind"],
        "label": c.get("label"),
        "acquisition": c.get("acquisition"),
        "state": c["state"],
        "health": c.get("health"),
        "allowed_purposes": c.get("allowed_purposes") or [],
        "external_identity": c.get("external_identity") or {},
        "current_credential_version_id": c.get("current_credential_version_id"),
        "revocation_epoch": c.get("revocation_epoch"),
        "created_at": _ts(c, "created_at"),
        "updated_at": _ts(c, "updated_at"),
    }


def session_view(s: dict) -> dict:
    h = s.get("harness") or {}
    return {
        "id": s["id"],
        "workspace_id": s["workspace_id"],
        "role": s.get("role"),
        "title": s.get("title"),
        "labels": s.get("labels") or [],
        "lifecycle": s.get("lifecycle"),
        "project_version_id": s.get("project_version_id"),
        "projectless_spec": s.get("projectless_spec") or {},
        "harness": {
            "provider_id": h.get("provider_id"),
            "model": h.get("model"),
            "effort": h.get("effort"),
        },
        "linked_from_session_id": s.get("linked_from_session_id"),
        "active_turn_id": s.get("active_turn_id"),
        "version": s.get("version"),
        "created_at": _ts(s, "created_at"),
        "updated_at": _ts(s, "updated_at"),
        "closed_at": _ts(s, "closed_at"),
    }


def message_view(m: dict) -> dict:
    return {
        "id": m["id"],
        "session_id": m["session_id"],
        "ordinal": m["ordinal"],
        "author": m.get("author"),
        "role": m.get("role"),
        "routing": m.get("routing"),
        "content": m.get("content") or {},
        "attachment_refs": m.get("attachment_refs") or [],
        "reply_to_message_id": m.get("reply_to_message_id"),
        "routed_turn_id": m.get("routed_turn_id"),
        "created_at": _ts(m, "created_at"),
    }


def turn_view(t: dict) -> dict:
    return {
        "id": t["id"],
        "session_id": t["session_id"],
        "ordinal": t["ordinal"],
        "message_id": t.get("message_id"),
        "state": t["state"],
        "reason": t.get("reason"),
        "outcome": t.get("outcome") or {},
        "retry_of_turn_id": t.get("retry_of_turn_id"),
        "cancel_intent": t.get("cancel_intent"),
        "version": t.get("version"),
        "created_at": _ts(t, "created_at"),
        "completed_at": _ts(t, "completed_at"),
    }


def event_view(e: dict) -> dict:
    return {
        "seq": e["seq"],
        "local_seq": e.get("local_seq"),
        "id": e["id"],
        "type": e["type"],
        "session_id": e["session_id"],
        "turn_id": e.get("turn_id"),
        "execution_id": e.get("execution_id"),
        "executor_lease_id": e.get("executor_lease_id"),
        "source": e.get("source"),
        "payload": e.get("payload") or {},
        "recorded_at": _ts(e, "recorded_at"),
        "observed_at": _ts(e, "observed_at"),
    }


def lease_view(lease: dict) -> dict:
    return {
        "id": lease["id"],
        "session_id": lease["session_id"],
        "backend": lease.get("backend"),
        "state": lease["state"],
        "generation": lease.get("generation"),
        "created_at": _ts(lease, "created_at"),
        "updated_at": _ts(lease, "updated_at"),
        # `handle` carries backend-opaque refs + grant digests — withheld.
    }


def changeset_view(c: dict) -> dict:
    return {
        "id": c["id"],
        "session_id": c["session_id"],
        "worktree_id": c.get("worktree_id"),
        "worktree_generation": c.get("worktree_generation"),
        "subject_digest": c["subject_digest"],
        "manifest_version": c.get("manifest_version"),
        "source_turn_id": c.get("source_turn_id"),
        "repository": c.get("repository"),
        "base_sha": c.get("base_sha"),
        "head_sha": c.get("head_sha"),
        "capture_origin": c.get("capture_origin"),
        "automatic_eligible": c.get("automatic_eligible"),
        "manifest": c.get("manifest") or {},
        "created_at": _ts(c, "created_at"),
    }


def changeset_file_view(f: dict) -> dict:
    return {
        "path": f["path"],
        "file_type": f["file_type"],
        "mode": f.get("mode"),
        "content_digest": f.get("content_digest"),
        "symlink_target": f.get("symlink_target"),
        "blob_id": f.get("blob_id"),
    }


def delivery_step_view(s: dict) -> dict:
    return {
        "id": s["id"],
        "kind": s["kind"],
        "ordinal": s.get("ordinal"),
        "effect_id": s.get("effect_id"),
        "state": s["state"],
        "expected": s.get("expected") or {},
        "result": s.get("result") or {},
        "observed_at": _ts(s, "observed_at"),
    }


def delivery_view(d: dict, steps: list[dict] | None = None, gate: dict | None = None) -> dict:
    out = {
        "id": d["id"],
        "session_id": d["session_id"],
        "changeset_id": d["changeset_id"],
        "subject_digest": d["subject_digest"],
        "target": d.get("target") or {},
        "transport": d["transport"],
        "ship_policy": d.get("ship_policy") or {},
        "state": d["state"],
        "effect_evidence": d.get("effect_evidence") or {},
        "authorizing_principal": d.get("authorizing_principal"),
        "connection_id": d.get("connection_id"),
        "version": d.get("version"),
        "created_at": _ts(d, "created_at"),
        "updated_at": _ts(d, "updated_at"),
    }
    if steps is not None:
        out["steps"] = [delivery_step_view(s) for s in steps]
    if gate is not None:
        out["merge_gate"] = gate
    return out


def merge_request_view(m: dict) -> dict:
    return {
        "id": m["id"],
        "delivery_id": m["delivery_id"],
        "changeset_id": m["changeset_id"],
        "subject_digest": m["subject_digest"],
        "expected_head_sha": m.get("expected_head_sha"),
        "expected_base_sha": m.get("expected_base_sha"),
        "merge_method": m["merge_method"],
        "state": m["state"],
        "gate_evidence": m.get("gate_evidence") or {},
        "result": m.get("result") or {},
        "created_at": _ts(m, "created_at"),
    }


def delegation_view(d: dict, inputs: list[dict] | None = None) -> dict:
    out = {
        "id": d["id"],
        "parent_session_id": d["parent_session_id"],
        "child_session_id": d["child_session_id"],
        "role": d["role"],
        "state": d["state"],
        "result_contract": d.get("result_contract") or {},
        "input_digest": d.get("input_digest"),
        "budget": d.get("budget") or {},
        "created_at": _ts(d, "created_at"),
        "updated_at": _ts(d, "updated_at"),
    }
    if inputs is not None:
        out["inputs"] = [
            {
                "kind": i["kind"],
                "ref": i["ref"],
                "digest": i.get("digest"),
            }
            for i in inputs
        ]
    return out


def delegation_result_view(r: dict | None) -> dict | None:
    if r is None:
        return None
    return {
        "id": r["id"],
        "delegation_id": r["delegation_id"],
        "child_session_id": r["child_session_id"],
        "completing_turn_id": r.get("completing_turn_id"),
        "contract_version": r.get("contract_version"),
        "subject_digest": r.get("subject_digest"),
        "head_sha": r.get("head_sha"),
        "verdict": r.get("verdict"),
        "validation_status": r["validation_status"],
        "value": r.get("value") or {},
        "evidence_refs": r.get("evidence_refs") or [],
        "published_at": _ts(r, "published_at"),
    }


def wait_view(w: dict) -> dict:
    return {
        "id": w["id"],
        "delegation_id": w["delegation_id"],
        "subscriber_session_id": w["subscriber_session_id"],
        "predicate": w.get("predicate") or {},
        "state": w["state"],
        "deadline_at": _ts(w, "deadline_at"),
        "satisfied_by_result_id": w.get("satisfied_by_result_id"),
        "created_at": _ts(w, "created_at"),
    }


def job_view(j: dict) -> dict:
    return {
        "id": j["id"],
        "kind": j["kind"],
        "state": j["state"],
        "target_family": j.get("target_family"),
        "target_id": j.get("target_id"),
        "attempts": j.get("attempts"),
        "result": j.get("result") or {},
        "last_error": _safe_job_error(j),
        "created_at": _ts(j, "created_at"),
        "updated_at": _ts(j, "updated_at"),
        # payload/holder never leave the API.
    }


def _safe_job_error(j: dict) -> dict | None:
    err = j.get("last_error")
    if not err:
        return None
    if isinstance(err, dict):
        return {"code": err.get("code"), "message": err.get("message")}
    return {"message": str(err)[:500]}


def operation_view(job: dict) -> dict:
    """A Job projected as an Operation — evidence view, not another
    authority."""
    return {
        "operation_id": job.get("effect_id") or job["id"],
        "job_id": job["id"],
        "kind": job["kind"],
        "state": job["state"],
        "target": {"family": job.get("target_family"), "id": job.get("target_id")},
        "attempts": job.get("attempts"),
        "result": job.get("result") or {},
        "created_at": _ts(job, "created_at"),
    }


def model_view(m: Any) -> dict:
    if isinstance(m, dict):
        return {
            "id": m.get("id"),
            "provider_id": m.get("provider_id"),
            "model": m.get("model"),
            "free": m.get("free"),
            "source_connection_id": m.get("source_connection_id"),
            "capabilities": m.get("capabilities") or {},
        }
    return {
        "id": getattr(m, "id", None),
        "provider_id": getattr(m, "provider_id", None),
        "model": getattr(m, "model", None),
        "free": getattr(m, "free", None),
        "source_connection_id": getattr(m, "source_connection_id", None),
        "capabilities": getattr(m, "capabilities", None) or {},
    }
