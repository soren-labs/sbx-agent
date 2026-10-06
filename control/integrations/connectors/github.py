"""GitHub manual token connector: identity probe and repo-scoped capability."""

from __future__ import annotations

from typing import Any

import httpx

from control.integrations.connectors.base import Observation, require

KIND = "github"
FORMAT = "github_token/v1"
API = "https://api.github.com"


def normalize(credential: dict[str, Any]) -> dict[str, Any]:
    require(credential, "token", min_len=20)
    return {"token": credential["token"].strip()}


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "sbx-browser",
    }


def _rate_limited(response: Any) -> float | None:
    if response.status_code in (403, 429) and response.headers.get("x-ratelimit-remaining") == "0":
        return float(response.headers.get("retry-after") or 60)
    return None


def validate(
    credential: dict[str, Any], *, repositories: list[str] | None = None, client: Any = None
) -> Observation:
    """Capability probe: the token must reach the configured repositories.

    ``GET /user`` only supplies an identity. GitHub App installation tokens (``ghs_``)
    get ``403 Resource not accessible by integration`` there although they can be
    repository-capable, so access is decided by repository/installation probes; only
    a ``401`` (bad or expired credentials) rejects the token outright.
    """
    http = client or httpx
    headers = _headers(credential["token"])
    try:
        user = http.get(f"{API}/user", headers=headers, timeout=20)
        if (wait := _rate_limited(user)) is not None:
            return Observation(
                "degraded", details={"reason": "github_rate_limited"}, retry_after=wait
            )
        if user.status_code == 401:
            return Observation(
                "invalid", details={"reason": "github_rejected_token", "status": 401}
            )
        if user.status_code >= 500:
            return Observation(
                "error", details={"reason": "github_error", "status": user.status_code}
            )
        login = user.json().get("login") if user.status_code == 200 else None
        scopes = [
            s.strip() for s in (user.headers.get("x-oauth-scopes") or "").split(",") if s.strip()
        ]
        installation: list[str] | None = None
        if login is None:
            listing = http.get(
                f"{API}/installation/repositories",
                headers=headers,
                params={"per_page": 100},
                timeout=20,
            )
            if listing.status_code == 401:
                return Observation(
                    "invalid", details={"reason": "github_rejected_token", "status": 401}
                )
            if listing.status_code == 200:
                installation = sorted(
                    r["full_name"]
                    for r in listing.json().get("repositories", [])
                    if "full_name" in r
                )
        repos: dict[str, Any] = {}
        for full_name in repositories or []:
            r = http.get(f"{API}/repos/{full_name}", headers=headers, timeout=20)
            if r.status_code >= 500:
                return Observation(
                    "error", details={"reason": "github_error", "status": r.status_code}
                )
            visible = r.status_code == 200
            perms = (r.json().get("permissions") if visible else None) or {}
            # Installation tokens carry no per-user permission block: push is then
            # unknown (None) until the Delivery worker's push is accepted or refused.
            push = bool(perms.get("push")) if perms else (None if visible else False)
            repos[full_name] = {"visible": visible, "push": push, "admin": bool(perms.get("admin"))}
    except httpx.HTTPError as exc:
        return Observation("error", details={"reason": f"github_unreachable:{type(exc).__name__}"})
    token_kind = "user" if login else ("installation" if installation is not None else "unknown")
    details: dict[str, Any] = {
        "token_kind": token_kind,
        "scopes": scopes,
        "repositories": repos,
        "probe": "GET /user" if login else "repository_capability",
    }
    if login is None:
        reachable = any(r["visible"] for r in repos.values()) or bool(installation)
        if not reachable:
            reason = "github_no_repository_access" if repositories else "github_rejected_token"
            return Observation(
                "invalid", details={**details, "reason": reason, "status": user.status_code}
            )
        if installation is not None:
            details["installation_repositories"] = installation[:100]
    # Capability is resource-scoped: no universal PR/merge claim.
    return Observation("ready", external_identity=login, details=details)
