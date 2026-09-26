"""``RemoteGitHub`` REST/GraphQL client seams — in particular the
``update_pull(draft=False)`` mark-ready-for-review path.

REST cannot clear ``draft`` on an open pull request, so the client uses the
``markPullRequestReadyForReview`` GraphQL mutation. The GraphQL
``PullRequest`` type exposes ``isDraft`` — the REST field name ``draft``
does not exist there and fails the whole mutation (SOR-221 acceptance).
These tests pin the exact selection set and the response normalization so
the field name cannot regress to the REST spelling.
"""

from __future__ import annotations

from typing import Any

import pytest
from control.github_remote import RemoteGitHub, RemoteGitHubError

_API = "https://api.github.com"
_SLUG = "o/r"
_PULL_URL = "https://github.com/o/r/pull/34"


def _pull(number: int = 34, *, draft: bool = True) -> dict[str, Any]:
    return {
        "number": number,
        "state": "open",
        "draft": draft,
        "node_id": "PR_node",
        "html_url": _PULL_URL,
    }


def _graphql_ok(number: int = 34) -> dict[str, Any]:
    return {
        "data": {
            "markPullRequestReadyForReview": {
                "pullRequest": {
                    "number": number,
                    "state": "OPEN",
                    "isDraft": False,
                    "url": _PULL_URL,
                }
            }
        }
    }


class _Response:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> Any:
        return self._payload


class _StubClient:
    """httpx-shaped stub: records requests, routes canned REST responses by
    ``(method, path-suffix)`` and answers ``/graphql`` from ``graphql``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.routes: dict[tuple[str, str], Any] = {}
        self.graphql: Any = None

    def request(self, method: str, url: str, headers: Any = None, json: Any = None) -> _Response:
        self.calls.append((method, url, json))
        if url.endswith("/graphql"):
            return _Response(self.graphql(json) if callable(self.graphql) else self.graphql)
        for (m, suffix), payload in self.routes.items():
            if method == m and url.endswith(suffix):
                return _Response(payload)
        raise AssertionError(f"unexpected request {method} {url}")

    def graphql_bodies(self) -> list[Any]:
        return [body for _m, url, body in self.calls if url.endswith("/graphql")]


def _remote(client: _StubClient) -> RemoteGitHub:
    return RemoteGitHub(token=None, api_url=_API, client=client)


class TestMarkPullReady:
    def test_draft_false_marks_ready_via_graphql_isdraft(self) -> None:
        client = _StubClient()
        client.routes[("GET", "/pulls/34")] = _pull()
        client.graphql = _graphql_ok()

        data = _remote(client).update_pull(_SLUG, 34, draft=False)

        bodies = client.graphql_bodies()
        assert len(bodies) == 1
        query = bodies[0]["query"]
        assert "markPullRequestReadyForReview" in query
        assert "isDraft" in query
        # GraphQL has no ``draft`` field — the REST spelling must not leak.
        assert "draft" not in query.replace("isDraft", "")
        assert bodies[0]["variables"] == {"id": "PR_node"}
        assert data == {
            "number": 34,
            "state": "open",
            "draft": False,
            "html_url": _PULL_URL,
            "merged": False,
        }

    def test_draft_false_on_ready_pr_skips_graphql(self) -> None:
        client = _StubClient()
        client.routes[("GET", "/pulls/34")] = _pull(draft=False)

        data = _remote(client).update_pull(_SLUG, 34, draft=False)

        assert client.graphql_bodies() == []
        assert data["draft"] is False

    def test_field_updates_without_draft_never_touch_graphql(self) -> None:
        client = _StubClient()
        client.routes[("PATCH", "/pulls/34")] = {**_pull(), "title": "renamed"}

        data = _remote(client).update_pull(_SLUG, 34, title="renamed")

        assert [m for m, _u, _b in client.calls] == ["PATCH"]
        assert data["title"] == "renamed"

    def test_graphql_errors_surface_repo_unavailable(self) -> None:
        client = _StubClient()
        client.routes[("GET", "/pulls/34")] = _pull()
        client.graphql = {"errors": [{"message": "Something went wrong"}]}

        with pytest.raises(RemoteGitHubError) as excinfo:
            _remote(client).update_pull(_SLUG, 34, draft=False)

        assert excinfo.value.code == "repo_unavailable"
        assert "mark PR #34 ready for review failed" in str(excinfo.value)

    def test_draft_pr_without_node_id_fails_closed(self) -> None:
        client = _StubClient()
        pull = _pull()
        pull["node_id"] = None
        client.routes[("GET", "/pulls/34")] = pull

        with pytest.raises(RemoteGitHubError) as excinfo:
            _remote(client).update_pull(_SLUG, 34, draft=False)

        assert excinfo.value.code == "repo_unavailable"
        assert client.graphql_bodies() == []
