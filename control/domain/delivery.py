from control.domain.errors import require

DEFAULT_POLICY = {
    "draft": True,
    "automatic": False,
    "required_roles": {"review": 1},
    "required_checks": [],
    "merge_methods": ["squash"],
    "require_base_unchanged": False,
}


def merge_reasons(delivery, changeset, results, remote):
    reasons = []
    if changeset["state"] != "ready" or changeset["subject_digest"] != delivery["subject_digest"]:
        reasons.append("stale_subject")
    if delivery["mapped_head"] != remote.get("head") or delivery["remote_head"] != remote.get(
        "head"
    ):
        reasons.append("remote_head_changed")
    policy = delivery["policy"]
    if policy.get("require_base_unchanged"):
        # GitHub's head-CAS merge does not atomically enforce base stability.
        reasons.append("unsupported_base_precondition")
    if remote.get("draft") or not remote.get("mergeable", False):
        reasons.append("remote_not_mergeable")
    valid = [
        r
        for r in results
        if r["validated"]
        and r["subject_digest"] == delivery["subject_digest"]
        and (not r["head_sha"] or r["head_sha"] == changeset.get("head_sha"))
    ]
    if any(r["verdict"] == "request_changes" for r in valid):
        reasons.append("requested_changes")
    for role, count in policy.get("required_roles", {}).items():
        kind = {"review": "ReviewAssessment", "test": "TestResult"}.get(role, role)
        passing = [
            r
            for r in valid
            if r["kind"] == kind and r["verdict"] in {"approve", "pass"} and r.get("independent")
        ]
        if len({r["child_session_id"] for r in passing}) < count:
            reasons.append("missing_" + role)
    for name in policy.get("required_checks", []):
        if remote.get("checks", {}).get(name) != "success":
            reasons.append("missing_check:" + name)
    return reasons


def validate_transport(value):
    require(value in {"pull_request", "git_branch", "export"}, "unsupported_capability")
