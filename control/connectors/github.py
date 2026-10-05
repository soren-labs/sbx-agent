"""GitHub connector: manual personal-token validation via /user (read-only
identity probe) + resource-scoped repo access check."""

from __future__ import annotations

import httpx

from control.connectors.base import ConnectorResult

_API = "https://api.github.com"


class GithubConnector:
    kind = "github"

    def validate(self, fmt: str, payload: dict) -> ConnectorResult:
        if fmt != "personal_token":
            return ConnectorResult(
                ok=False, reason="unsupported_format", message="expected personal_token"
            )
        token = payload.get("token") or ""
        if not token:
            return ConnectorResult(ok=False, reason="invalid_payload")
        try:
            resp = httpx.get(
                f"{_API}/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=15,
            )
        except httpx.HTTPError as exc:
            return ConnectorResult(
                ok=False,
                reason="probe_failed",
                message=f"GitHub unreachable ({type(exc).__name__})",
            )
        if resp.status_code in (401, 403):
            return ConnectorResult(
                ok=False, reason="auth_failed", message="GitHub rejected the token"
            )
        if resp.status_code != 200:
            return ConnectorResult(
                ok=False,
                reason="probe_failed",
                message=f"GitHub probe status {resp.status_code}",
            )
        body = resp.json()
        scopes = sorted(
            s.strip() for s in (resp.headers.get("x-oauth-scopes") or "").split(",") if s.strip()
        )
        return ConnectorResult(
            ok=True,
            external_identity={"login": body.get("login"), "id": body.get("id")},
            capabilities={
                "delivery_worker": {
                    "scopes": scopes,
                    "can_push": "repo" in scopes
                    or "public_repo" in scopes
                    or not scopes,  # fine-grained tokens list no scopes
                }
            },
        )

    def check_repo_access(self, token: str, repo: str) -> ConnectorResult:
        """Resource-scoped probe: can this token see/own the named repo?"""
        try:
            resp = httpx.get(
                f"{_API}/repos/{repo}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=15,
            )
        except httpx.HTTPError as exc:
            return ConnectorResult(
                ok=False,
                reason="probe_failed",
                message=f"GitHub unreachable ({type(exc).__name__})",
            )
        if resp.status_code == 404:
            return ConnectorResult(
                ok=False, reason="repo_not_found", message=f"cannot access {repo}"
            )
        if resp.status_code in (401, 403):
            return ConnectorResult(ok=False, reason="auth_failed")
        perms = resp.json().get("permissions") or {}
        return ConnectorResult(
            ok=True,
            capabilities={
                "repo": repo,
                "push": bool(perms.get("push")),
                "pull": bool(perms.get("pull", True)),
            },
        )
