from control.domain.errors import require
from control.domain.events import digest


def command(repo, principal, workspace, kind, key, body, perform):
    require(bool(key) and len(key) <= 200, "invalid_cursor")
    fingerprint = digest(body)
    repo.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (f"{principal.user_id}:{workspace}:{kind}:{key}",),
    )
    previous = repo.one(
        "SELECT * FROM command_deduplication WHERE principal_id=%s AND workspace_id=%s "
        "AND command_kind=%s AND key=%s",
        (principal.user_id, workspace, kind, key),
    )
    if previous:
        require(previous["fingerprint"] == fingerprint, "idempotency_conflict")
        return previous["response"]
    response = perform()
    repo.execute(
        "INSERT INTO command_deduplication "
        "(principal_id,workspace_id,command_kind,key,fingerprint,response) "
        "VALUES(%s,%s,%s,%s,%s,%s)",
        (principal.user_id, workspace, kind, key, fingerprint, response),
    )
    return response
