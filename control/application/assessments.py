"""Pure evidence joins shared by command gates and read projections."""


def result_rows(repo, changeset):
    rows = repo.all(
        "SELECT r.*,d.parent_session_id,d.child_session_id AS assigned_child FROM dele"
        "gation_results r JOIN delegations d ON d.id=r.delegation_id WHERE d.changeset"
        "_id=%s",
        (changeset["id"],),
    )
    for row in rows:
        child = repo.one(
            "SELECT w.id AS worktree,n.native_id,n.lineage_id,n.provider_id,e.id AS ex"
            "ecution,e.lease_id FROM worktrees w JOIN native_context_bindings n ON n.s"
            "ession_id=w.session_id JOIN executions e ON e.turn_id=%s WHERE w.session_"
            "id=%s ORDER BY n.created_at DESC LIMIT 1",
            (row["completing_turn_id"], row["child_session_id"]),
        )
        parent = repo.one(
            "SELECT w.id AS worktree,n.native_id,n.lineage_id,n.provider_id FROM workt"
            "rees w JOIN native_context_bindings n ON n.session_id=w.session_id WHERE "
            "w.session_id=%s ORDER BY n.created_at DESC LIMIT 1",
            (row["parent_session_id"],),
        )
        reused = repo.one(
            "SELECT e.id FROM executions e JOIN turns t ON t.id=e.turn_id WHERE t.sess"
            "ion_id=%s AND (e.id=%s OR e.lease_id=%s)",
            (
                row["parent_session_id"],
                child["execution"] if child else None,
                child["lease_id"] if child else None,
            ),
        )
        row["independent"] = bool(
            child
            and parent
            and not reused
            and row["assigned_child"] != row["parent_session_id"]
            and child["worktree"] != parent["worktree"]
            and child["lineage_id"] != parent["lineage_id"]
            and (
                child["provider_id"] != parent["provider_id"]
                or child["native_id"] != parent["native_id"]
            )
        )
    return rows


def current_policy(repo, delivery):
    from control.domain.delivery import DEFAULT_POLICY

    row = repo.one(
        "SELECT latest.spec FROM sessions s JOIN project_versions pinned ON pinned.id="
        "s.project_version_id JOIN projects p ON p.id=pinned.project_id JOIN project_v"
        "ersions latest ON latest.id=p.current_version_id WHERE s.id=%s",
        (delivery["session_id"],),
    )
    policy = dict(delivery["policy"])
    if row:
        current = {**DEFAULT_POLICY, **row["spec"].get("ship_policy", {})}
        policy["required_roles"] = {
            role: max(
                policy.get("required_roles", {}).get(role, 0),
                current.get("required_roles", {}).get(role, 0),
            )
            for role in set(policy.get("required_roles", {}))
            | set(current.get("required_roles", {}))
        }
        policy["required_checks"] = sorted(
            set(policy.get("required_checks", [])) | set(current.get("required_checks", []))
        )
        policy["merge_methods"] = [
            method for method in policy["merge_methods"] if method in current["merge_methods"]
        ]
        policy["require_base_unchanged"] = policy.get(
            "require_base_unchanged", False
        ) or current.get("require_base_unchanged", False)
    return policy
