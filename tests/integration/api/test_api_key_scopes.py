"""API key scopes are enforced on every route: read keys never mutate (RFC 06)."""

from __future__ import annotations

import uuid

import pytest
from control.domain.ids import new_id
from control.security.passwords import new_token, token_hash
from tests.support.api import ApiStack, User, inference

SPEC = {
    "repository": None,
    "checks": [{"name": "unit", "argv": ["python3", "-c", "print('ok')"]}],
    "defaults": {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}},
}


@pytest.fixture
def stack(db, tmp_path):
    s = ApiStack(db, tmp_path)
    yield s
    s.shutdown()


def _mint(user: User, scopes: list[str] | None) -> tuple[str, dict]:
    body: dict = {"name": f"key-{uuid.uuid4().hex[:6]}"}
    if scopes is not None:
        body["scopes"] = scopes
    created = user.post("/api/api-keys", body)
    assert created.status_code == 201, created.text
    return created.json()["key"], created.json()


def _call(http, method: str, path: str, key: str, body: dict | None = None):
    headers = {"Authorization": f"Bearer {key}", "Idempotency-Key": uuid.uuid4().hex}
    return http.request(method, path, json=body if body is not None else {}, headers=headers)


def _mutations(user: User, project_id: str, session_id: str, connection_id: str) -> list:
    ws = user.workspace_id
    return [
        ("POST", f"/api/workspaces/{ws}/projects", {"slug": "evil", "name": "x", "spec": SPEC}),
        ("PATCH", f"/api/projects/{project_id}", {"name": "renamed", "expected_version": 1}),
        ("POST", f"/api/projects/{project_id}/versions", {"spec": SPEC, "expected_version": 1}),
        (
            "POST",
            f"/api/workspaces/{ws}/connections",
            {"kind": "opencode_zen", "label": "x", "credential": {"api_key": "REDACTED-123456"}},
        ),
        ("PATCH", f"/api/connections/{connection_id}", {"label": "renamed"}),
        ("DELETE", f"/api/connections/{connection_id}", None),
        (
            "POST",
            f"/api/connections/{connection_id}/credential-versions",
            {"credential": {"api_key": "REDACTED-654321"}},
        ),
        ("POST", f"/api/connections/{connection_id}/validations", {}),
        ("POST", f"/api/workspaces/{ws}/sessions", {"project_id": project_id}),
        ("PATCH", f"/api/sessions/{session_id}", {"title": "x", "expected_version": 1}),
        ("POST", f"/api/sessions/{session_id}/messages", {"content": "hi"}),
        ("POST", f"/api/sessions/{session_id}/archives", {}),
        ("POST", f"/api/sessions/{session_id}/closures", {}),
        ("POST", f"/api/sessions/{session_id}/executor/activations", {}),
        ("POST", f"/api/sessions/{session_id}/executor/releases", {}),
        ("PUT", f"/api/sessions/{session_id}/files", {"path": "a", "content": "b"}),
        ("POST", f"/api/sessions/{session_id}/terminals", {}),
        ("POST", f"/api/sessions/{session_id}/changesets", {}),
        ("POST", f"/api/sessions/{session_id}/delegations", {"brief": "x"}),
        ("POST", "/api/turns/turn_unknown/cancellations", {}),
        ("POST", "/api/turns/turn_unknown/retries", {}),
        ("POST", "/api/turns/turn_unknown/acknowledgements", {}),
        ("POST", "/api/changesets/cs_unknown/deliveries", {"transport": "branch"}),
        ("POST", "/api/changesets/cs_unknown/applications", {}),
        ("POST", "/api/deliveries/dlv_unknown/retries", {}),
        ("POST", "/api/deliveries/dlv_unknown/refreshes", {}),
        ("POST", "/api/deliveries/dlv_unknown/merge-requests", {"method": "squash"}),
        ("POST", "/api/delegations/del_unknown/cancellations", {}),
    ]


def _fixtures(stack: ApiStack, user: User) -> tuple[str, str, str]:
    con = user.connect("inference_api", inference("zen-api-key-for-scopes-0001"))
    stack.drain()
    project = user.post(
        f"/api/workspaces/{user.workspace_id}/projects", {"slug": "demo", "name": "D", "spec": SPEC}
    )
    assert project.status_code == 201, project.text
    session = user.post(
        f"/api/workspaces/{user.workspace_id}/sessions", {"project_id": project.json()["id"]}
    )
    assert session.status_code == 201, session.text
    return project.json()["id"], session.json()["session"]["id"], con["id"]


def _counts(stack: ApiStack) -> dict[str, object]:
    tables = ("projects", "project_versions", "connections", "credential_versions", "sessions")
    tables += ("messages", "turns", "changesets", "deliveries", "api_keys", "delegations")

    def snap(u):
        rows = {t: u.count(t, {}) for t in tables}
        rows["rows"] = repr(
            [u.find(t, {}, order="created_at") for t in ("projects", "connections", "sessions")]
        )
        rows["jobs"] = u.count("jobs", {})
        return rows

    return stack.db.read(snap)


def test_read_only_key_cannot_mutate_anything(stack) -> None:
    user = User(stack)
    project_id, session_id, connection_id = _fixtures(stack, user)
    key, view = _mint(user, ["read"])
    assert view["scopes"] == ["read"]
    http = stack.client()
    before = _counts(stack)
    for method, path, body in _mutations(user, project_id, session_id, connection_id):
        r = _call(http, method, path, key, body)
        assert r.status_code == 403, (method, path, r.status_code, r.text)
        assert r.json()["error"]["code"] == "forbidden", (method, path)
    for method, path in (("POST", "/api/api-keys"), ("POST", "/api/auth/password-changes")):
        assert _call(http, method, path, key, {"name": "x"}).status_code == 403
    assert _call(http, "DELETE", f"/api/api-keys/{view['id']}", key).status_code == 403
    assert _counts(stack) == before, "a forbidden request must not have side effects"
    # Reads still work for the read key.
    assert _call(http, "GET", f"/api/projects/{project_id}", key).status_code == 200
    assert _call(http, "GET", f"/api/sessions/{session_id}", key).status_code == 200


def test_write_key_mutates_but_cannot_mint_or_revoke_keys(stack) -> None:
    user = User(stack)
    project_id, _session_id, _ = _fixtures(stack, user)
    key, view = _mint(user, ["write"])
    http = stack.client()
    created = _call(
        http,
        "POST",
        f"/api/workspaces/{user.workspace_id}/projects",
        key,
        {"slug": "by-key", "name": "K", "spec": SPEC},
    )
    assert created.status_code == 201, created.text
    assert _call(http, "GET", f"/api/projects/{project_id}", key).status_code == 200
    assert _call(http, "POST", "/api/api-keys", key, {"name": "escalate"}).status_code == 403
    assert _call(http, "DELETE", f"/api/api-keys/{view['id']}", key).status_code == 403
    changed = _call(
        http,
        "POST",
        "/api/auth/password-changes",
        key,
        {"current_password": user.password, "new_password": "another long password 1"},
    )
    assert changed.status_code == 403


def test_full_scope_key_and_cookie_keep_working(stack) -> None:
    user = User(stack)
    key, view = _mint(user, None)
    assert view["scopes"] == ["*"]
    http = stack.client()
    minted = _call(http, "POST", "/api/api-keys", key, {"name": "child", "scopes": ["read"]})
    assert minted.status_code == 201, minted.text
    cookie = user.post(
        f"/api/workspaces/{user.workspace_id}/projects", {"slug": "c", "name": "C", "spec": SPEC}
    )
    assert cookie.status_code == 201, "cookie sessions are full-scope"


@pytest.mark.parametrize("scopes", [[], ["admin"], ["read", "root"], "read", [1]])
def test_minting_rejects_unknown_or_empty_scopes(stack, scopes) -> None:
    user = User(stack)
    r = user.post("/api/api-keys", {"name": "bad", "scopes": scopes})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_failed"
    assert stack.db.read(lambda u: u.count("api_keys", {})) == 0


def test_key_with_unknown_scope_in_db_grants_nothing(stack) -> None:
    user = User(stack)
    key = new_token("sbx_key_")
    user_id = user.me["user"]["id"]
    stack.db.run(
        lambda u: u.insert(
            "api_keys",
            {
                "id": new_id("api_key"),
                "user_id": user_id,
                "workspace_id": user.workspace_id,
                "name": "legacy",
                "key_hash": token_hash(key),
                "display_prefix": key[:14],
                "scopes": ["admin"],
            },
        )
    )
    http = stack.client()
    assert _call(http, "GET", "/api/me", key).status_code == 403
    assert (
        _call(
            http,
            "POST",
            f"/api/workspaces/{user.workspace_id}/projects",
            key,
            {"slug": "x", "name": "x", "spec": SPEC},
        ).status_code
        == 403
    )
