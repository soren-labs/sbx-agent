from control.domain.errors import require


def advance(repo, workspace, resource, holder, seconds=120):
    row = repo.one(
        "INSERT INTO resource_fences(workspace_id,resource,generation,holder,expires_at) "
        "VALUES(%s,%s,1,%s,now()+%s*interval '1 second') ON CONFLICT(workspace_id,resource) "
        "DO UPDATE SET generation=resource_fences.generation+1,holder=excluded.holder,"
        "expires_at=excluded.expires_at WHERE resource_fences.expires_at<now() "
        "OR resource_fences.holder=excluded.holder RETURNING generation",
        (workspace, resource, holder, seconds),
    )
    require(row is not None, "waiting_capacity")
    return row["generation"]


def verify(repo, workspace, resource, holder, generation):
    row = repo.one(
        "SELECT generation,holder,expires_at>now() AS valid FROM resource_fences "
        "WHERE workspace_id=%s AND resource=%s FOR UPDATE",
        (workspace, resource),
    )
    require(
        row and row["valid"] and row["holder"] == holder and row["generation"] == generation,
        "version_conflict",
    )
