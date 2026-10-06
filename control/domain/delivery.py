"""Delivery lifecycle and the exact-subject merge gate (RFC 05). Pure rules only."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from control.domain.lifecycle import Lifecycle, table

DELIVERY = Lifecycle(
    "delivery",
    table(
        {
            "pending": ("executing", "blocked", "cancelled"),
            "executing": ("blocked", "succeeded", "failed"),
            "blocked": ("executing", "cancelled"),
            "failed": ("pending",),
        }
    ),
    frozenset({"succeeded", "cancelled"}),
)
MERGE_REQUEST_STATES = ("pending", "executing", "blocked", "succeeded", "failed", "cancelled")
OBSERVATION_MAX_AGE = timedelta(minutes=10)


def target_ref(session_id: str, changeset_id: str) -> str:
    return f"sbx/{session_id}/{changeset_id}"


def evaluate_merge_gate(
    *,
    changeset: dict[str, Any],
    delivery: dict[str, Any],
    merge_request: dict[str, Any] | None,
    results: list[dict[str, Any]],
    policy: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Eligibility from typed projections + timestamped remote observation."""
    reasons: list[str] = []
    subject = changeset.get("subject_digest")
    head = delivery.get("remote_head_sha")
    if changeset.get("state") != "ready":
        reasons.append("changeset_not_sealed")
    if delivery.get("state") != "succeeded":
        reasons.append("delivery_not_verified")
    if delivery.get("subject_digest") != subject:
        reasons.append("stale_subject")
    if not head or head != delivery.get("commit_sha"):
        reasons.append("remote_head_changed")
    observed = delivery.get("observed_at")
    if observed is None or now - observed > OBSERVATION_MAX_AGE:
        reasons.append("observation_stale")
    if (
        delivery.get("pr_state") not in (None, "open")
        and delivery.get("transport") == "pull_request"
    ):
        reasons.append("pull_request_not_open")
    if merge_request is not None:
        if merge_request["expected_head_sha"] != head:
            reasons.append("remote_head_changed")
        if merge_request["subject_digest"] != subject:
            reasons.append("stale_subject")
        if merge_request["expected_delivery_version"] != delivery.get("version"):
            reasons.append("version_conflict")
        if merge_request["method"] not in (policy.get("merge_methods", ["squash"]) or []):
            reasons.append("merge_method_not_allowed")
        if delivery.get("pr_draft") and not merge_request.get("mark_ready"):
            reasons.append("pull_request_is_draft")
    pinned = [
        r
        for r in results
        if r.get("subject_digest") == subject
        and r.get("validation_status") == "valid"
        and r.get("independent")
    ]
    for requirement in policy.get("required_results") or []:
        kind, count = requirement.get("kind"), int(requirement.get("count") or 1)
        want = requirement.get("verdict") or ("approve" if kind == "ReviewAssessment" else "pass")
        matching = [r for r in pinned if r.get("kind") == kind and r.get("verdict") == want]
        if len(matching) < count:
            reasons.append(f"missing_required_result:{kind}")
    if any(
        r.get("kind") == "ReviewAssessment" and r.get("verdict") == "request_changes"
        for r in pinned
    ):
        reasons.append("unresolved_request_changes")
    checks = {c.get("name"): c.get("conclusion") for c in (delivery.get("checks") or [])}
    for name in policy.get("required_checks") or []:
        if checks.get(name) != "success":
            reasons.append(f"check_not_passing:{name}")
    if policy.get("require_base_unchanged"):
        reasons.append("unsupported_base_stability")
    unique = sorted(set(reasons))
    return {
        "eligible": not unique,
        "reasons": unique,
        "subject_digest": subject,
        "head_sha": head,
        "observed_at": observed.isoformat() if observed else None,
    }
