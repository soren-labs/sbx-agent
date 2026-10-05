from control.domain.errors import require


def workspace(principal, workspace_id):
    require(workspace_id in principal.workspace_ids, "not_found")


def owned(repo, table, resource_id, principal, *, lock=False):
    # Table names are application constants, never request input.
    row = repo.one(
        f"SELECT * FROM {table} WHERE id=%s" + (" FOR UPDATE" if lock else ""),
        (resource_id,),
    )
    require(row is not None, "not_found")
    workspace(principal, row["workspace_id"])
    return row
