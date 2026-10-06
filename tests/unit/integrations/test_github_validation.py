"""GitHub token validation is capability-based: PATs and App installation tokens."""

from __future__ import annotations

from typing import Any

import httpx
from control.integrations.connectors import github

PAT = {"token": "ghp_" + "R" * 36}  # REDACTED placeholder shape, not a real token
INSTALLATION = {"token": "ghs_" + "R" * 36}


class FakeGitHub:
    """Deterministic stand-in for api.github.com keyed by request path."""

    def __init__(self, routes: dict[str, tuple[int, Any, dict[str, str]]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str, *, headers: dict[str, str], timeout: float, **_: Any) -> httpx.Response:
        path = url.removeprefix(github.API)
        self.calls.append(path)
        assert headers["Authorization"].startswith("Bearer ")
        status, body, extra = self.routes.get(path, (404, {"message": "Not Found"}, {}))
        if isinstance(status, Exception):
            raise status
        return httpx.Response(status, json=body, headers=extra, request=httpx.Request("GET", url))


INTEGRATION_403 = (403, {"message": "Resource not accessible by integration"}, {})


def test_pat_keeps_identity_scopes_and_repo_permissions() -> None:
    fake = FakeGitHub(
        {
            "/user": (200, {"login": "octo"}, {"x-oauth-scopes": "repo, workflow"}),
            "/repos/acme/demo": (200, {"permissions": {"push": True, "admin": False}}, {}),
        }
    )
    obs = github.validate(PAT, repositories=["acme/demo"], client=fake)
    assert obs.status == "ready" and obs.external_identity == "octo"
    assert obs.details["token_kind"] == "user" and obs.details["scopes"] == ["repo", "workflow"]
    assert obs.details["repositories"]["acme/demo"] == {
        "visible": True,
        "push": True,
        "admin": False,
    }
    assert "/installation/repositories" not in fake.calls


def test_pat_without_projects_is_ready() -> None:
    fake = FakeGitHub({"/user": (200, {"login": "octo"}, {})})
    assert github.validate(PAT, client=fake).status == "ready"


def test_installation_token_is_ready_despite_user_403() -> None:
    fake = FakeGitHub(
        {
            "/user": INTEGRATION_403,
            "/installation/repositories": (
                200,
                {"total_count": 1, "repositories": [{"full_name": "acme/demo"}]},
                {},
            ),
            "/repos/acme/demo": (200, {"full_name": "acme/demo"}, {}),
        }
    )
    obs = github.validate(INSTALLATION, repositories=["acme/demo"], client=fake)
    assert obs.status == "ready", obs.details
    assert obs.external_identity is None
    assert obs.details["token_kind"] == "installation"
    assert obs.details["repositories"]["acme/demo"]["visible"] is True
    assert obs.details["repositories"]["acme/demo"]["push"] is None, "unknown, not denied"
    assert obs.details["installation_repositories"] == ["acme/demo"]


def test_installation_token_without_projects_uses_installation_listing() -> None:
    fake = FakeGitHub(
        {
            "/user": INTEGRATION_403,
            "/installation/repositories": (200, {"repositories": [{"full_name": "a/b"}]}, {}),
        }
    )
    assert github.validate(INSTALLATION, client=fake).status == "ready"


def test_installation_token_without_access_to_configured_repo_is_invalid() -> None:
    fake = FakeGitHub(
        {
            "/user": INTEGRATION_403,
            "/installation/repositories": (200, {"repositories": [{"full_name": "other/x"}]}, {}),
            "/repos/acme/demo": (404, {"message": "Not Found"}, {}),
        }
    )
    obs = github.validate(INSTALLATION, repositories=["acme/demo"], client=fake)
    assert obs.status == "ready", "installation listing proves a working token"
    fake.routes["/installation/repositories"] = INTEGRATION_403
    obs = github.validate(INSTALLATION, repositories=["acme/demo"], client=fake)
    assert obs.status == "invalid" and obs.details["reason"] == "github_no_repository_access"


def test_bad_credentials_are_rejected() -> None:
    fake = FakeGitHub({"/user": (401, {"message": "Bad credentials"}, {})})
    obs = github.validate(PAT, repositories=["acme/demo"], client=fake)
    assert obs.status == "invalid" and obs.details["reason"] == "github_rejected_token"
    assert fake.calls == ["/user"]


def test_unusable_token_with_no_capability_is_invalid() -> None:
    fake = FakeGitHub({"/user": INTEGRATION_403, "/installation/repositories": INTEGRATION_403})
    obs = github.validate(INSTALLATION, client=fake)
    assert obs.status == "invalid" and obs.details["reason"] == "github_rejected_token"


def test_rate_limit_and_outage_are_not_reauth() -> None:
    limited = FakeGitHub(
        {"/user": (403, {"message": "rate"}, {"x-ratelimit-remaining": "0", "retry-after": "7"})}
    )
    obs = github.validate(PAT, client=limited)
    assert obs.status == "degraded" and obs.retry_after == 7.0
    down = FakeGitHub({"/user": (502, {}, {})})
    assert github.validate(PAT, client=down).status == "error"
    lost = FakeGitHub({"/user": (httpx.ConnectError("boom"), None, {})})
    assert github.validate(PAT, client=lost).status == "error"


def test_token_never_appears_in_details() -> None:
    fake = FakeGitHub({"/user": INTEGRATION_403, "/installation/repositories": INTEGRATION_403})
    obs = github.validate(INSTALLATION, repositories=["acme/demo"], client=fake)
    assert INSTALLATION["token"] not in repr(obs)
