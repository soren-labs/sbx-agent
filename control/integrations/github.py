"""GitHub pull request transport (REST + one GraphQL mutation for ready-for-review)."""

from __future__ import annotations

from typing import Any

import httpx

from control.domain.errors import DomainError

API = "https://api.github.com"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "sbx-agent",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _pr(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": data["number"],
        "url": data["html_url"],
        "node_id": data.get("node_id"),
        "head_sha": data["head"]["sha"],
        "head_ref": data["head"]["ref"],
        "base_ref": data["base"]["ref"],
        "state": "merged" if data.get("merged_at") else data["state"],
        "draft": bool(data.get("draft")),
        "mergeable": data.get("mergeable"),
        "body": data.get("body") or "",
    }


class GitHubHost:
    def __init__(self, client: Any = None) -> None:
        self.http = client or httpx.Client(timeout=30)

    def _check(self, response: httpx.Response) -> dict[str, Any] | list[Any]:
        if response.status_code in (401, 403):
            raise DomainError(
                "credential_invalid",
                "GitHub rejected the token for this operation",
                details={"status": response.status_code},
            )
        if response.status_code >= 400:
            raise DomainError(
                "delivery_unresolved",
                "GitHub API error",
                details={
                    "status": response.status_code,
                    "message": str(response.json().get("message", ""))[:200],
                },
                retryable=response.status_code >= 500,
            )
        return response.json()

    def find_pr(self, repo: str, token: str, head_ref: str) -> dict[str, Any] | None:
        owner = repo.split("/")[0]
        data = self._check(
            self.http.get(
                f"{API}/repos/{repo}/pulls",
                params={"head": f"{owner}:{head_ref}", "state": "all"},
                headers=_headers(token),
            )
        )
        return _pr(data[0]) if data else None

    def create_pr(
        self, repo: str, token: str, *, head_ref: str, base: str, title: str, body: str, draft: bool
    ) -> dict[str, Any]:
        payload = {"head": head_ref, "base": base, "title": title, "body": body, "draft": draft}
        return _pr(
            self._check(
                self.http.post(f"{API}/repos/{repo}/pulls", json=payload, headers=_headers(token))
            )
        )

    def get_pr(self, repo: str, token: str, number: int) -> dict[str, Any]:
        return _pr(
            self._check(
                self.http.get(f"{API}/repos/{repo}/pulls/{number}", headers=_headers(token))
            )
        )

    def checks(self, repo: str, token: str, sha: str) -> list[dict[str, Any]]:
        data = self._check(
            self.http.get(f"{API}/repos/{repo}/commits/{sha}/check-runs", headers=_headers(token))
        )
        status = self._check(
            self.http.get(f"{API}/repos/{repo}/commits/{sha}/status", headers=_headers(token))
        )
        out = [
            {"name": c["name"], "conclusion": c.get("conclusion") or c.get("status")}
            for c in data.get("check_runs", [])
        ]
        out += [
            {"name": s["context"], "conclusion": s["state"]} for s in status.get("statuses", [])
        ]
        return out

    def mark_ready(self, repo: str, token: str, pr: dict[str, Any]) -> None:
        query = "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{isDraft}}}"
        self._check(
            self.http.post(
                f"{API}/graphql",
                json={"query": query, "variables": {"id": pr["node_id"]}},
                headers=_headers(token),
            )
        )

    def merge(self, repo: str, token: str, number: int, *, sha: str, method: str) -> dict[str, Any]:
        response = self.http.put(
            f"{API}/repos/{repo}/pulls/{number}/merge",
            json={"sha": sha, "merge_method": method},
            headers=_headers(token),
        )
        if response.status_code == 409:
            raise DomainError("remote_head_changed", "GitHub refused the merge: head changed")
        if response.status_code == 405:
            raise DomainError(
                "gate_blocked",
                "GitHub refused the merge (not mergeable)",
                details={"message": str(response.json().get("message", ""))[:200]},
            )
        data = self._check(response)
        return {"merged": bool(data.get("merged")), "sha": data.get("sha")}
