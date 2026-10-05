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


def validate(
    credential: dict[str, Any], *, repositories: list[str] | None = None, client: Any = None
) -> Observation:
    http = client or httpx
    try:
        response = http.get(f"{API}/user", headers=_headers(credential["token"]), timeout=20)
    except httpx.HTTPError as exc:
        return Observation("error", details={"reason": f"github_unreachable:{type(exc).__name__}"})
    if response.status_code in (401, 403):
        return Observation(
            "invalid", details={"reason": "github_rejected_token", "status": response.status_code}
        )
    if response.status_code != 200:
        return Observation(
            "error", details={"reason": "github_error", "status": response.status_code}
        )
    login = response.json().get("login")
    scopes = [
        s.strip() for s in (response.headers.get("x-oauth-scopes") or "").split(",") if s.strip()
    ]
    repos: dict[str, Any] = {}
    for full_name in repositories or []:
        r = http.get(f"{API}/repos/{full_name}", headers=_headers(credential["token"]), timeout=20)
        perms = r.json().get("permissions", {}) if r.status_code == 200 else {}
        repos[full_name] = {
            "visible": r.status_code == 200,
            "push": bool(perms.get("push")),
            "admin": bool(perms.get("admin")),
        }
    # Capability is resource-scoped: no universal PR/merge claim.
    return Observation(
        "ready",
        external_identity=login,
        details={"scopes": scopes, "repositories": repos, "probe": "GET /user"},
    )
