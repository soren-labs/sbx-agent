"""Dedicated untrusted preview origin. No Console cookies or admin headers forwarded."""

from control.domain.errors import require
from control.domain.identity import Principal
from control.security.identity import hashed
from fastapi import FastAPI, Request
from fastapi.responses import Response


def create_preview_app(resources):
    r = resources
    app = FastAPI(docs_url=None, openapi_url=None)

    @app.api_route(
        "/p/{grant}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]
    )
    async def proxy(grant: str, path: str, request: Request):
        with r.uow.transaction() as repo:
            row = repo.one(
                "SELECT g.*,d.session_id,l.state AS lease_state,s.creator_id "
                "FROM preview_grants g JOIN service_desires d ON d.id=g.service_id "
                "JOIN sessions s ON s.id=d.session_id JOIN executor_leases l ON l.id=g.lease_id "
                "WHERE g.token_hash=%s AND g.expires_at>now()",
                (hashed(grant),),
            )
        require(row is not None and row["lease_state"] == "ready", "forbidden")
        principal = Principal(row["creator_id"], (row["workspace_id"],))
        client = r.io.client(principal, row["session_id"])
        require(client.hello["lease_id"] == row["lease_id"], "forbidden")
        content = await request.body()
        require(len(content) <= 4_000_000, "quota_exhausted")
        response = client.http.request(
            request.method,
            "/services/" + row["service_id"] + "/proxy/" + path,
            params=request.query_params,
            content=content,
            headers={
                "Content-Type": request.headers.get("content-type", "application/octet-stream")
            },
        )
        require(len(response.content) <= 4_000_000, "quota_exhausted")
        return Response(
            response.content,
            status_code=response.status_code,
            headers={
                "Content-Type": response.headers.get("content-type", "application/octet-stream"),
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    "sandbox allow-scripts allow-forms; frame-ancestors 'none'"
                ),
            },
        )

    return app
