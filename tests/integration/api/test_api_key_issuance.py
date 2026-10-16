"""API-key issuance is retry-safe and scoped; revocation is immediate (#230 AUD20/AUD21)."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

import httpx
import pytest
from tests.support.api import ApiStack, User

from sbx.sdk import SBXClient, SBXError


@pytest.fixture
def stack(db, tmp_path):
    s = ApiStack(db, tmp_path)
    yield s
    s.shutdown()


def _live_keys(stack: ApiStack, user: User) -> list[dict]:
    user_id = user.me["user"]["id"]
    return stack.db.read(
        lambda u: u.find("api_keys", {"user_id": user_id, "revoked_at": None}, order="created_at")
    )


def _with_key(http, method: str, path: str, key: str, body: dict | None = None):
    headers = {"Authorization": f"Bearer {key}", "Idempotency-Key": uuid.uuid4().hex}
    return http.request(method, path, json=body if body is not None else {}, headers=headers)


def _at_rest_text(stack: ApiStack) -> str:
    """Every row a key could leak into, as text (sealed replay copies are ciphertext)."""
    tables = ("api_keys", "audit_records", "command_deduplication", "outbox_messages")

    def read(u):
        out = []
        for t in tables:
            out += [r["j"] for r in u._all(f"SELECT row_to_json(x)::text AS j FROM {t} x")]
        return "\n".join(out)

    return stack.db.read(read)


class _LoseResponse(httpx.Client):
    """Forwards POST /api/api-keys to the server, then loses or replaces the response."""

    def __init__(self, inner: httpx.Client, failures: list[str]) -> None:
        self.inner, self.failures, self.keys = inner, list(failures), []

    def request(self, method, url, **kw):  # type: ignore[override]
        response = self.inner.request(method, url, **kw)
        if method == "POST" and str(url).endswith("/api/api-keys") and self.failures:
            self.keys.append(kw["headers"].get("Idempotency-Key"))
            failure = self.failures.pop(0)
            if failure == "timeout":
                raise httpx.ReadTimeout("response lost after commit")
            return httpx.Response(int(failure), json={"error": {"code": "bad_gateway"}})
        if method == "POST" and str(url).endswith("/api/api-keys"):
            self.keys.append(kw["headers"].get("Idempotency-Key"))
        return response


def _sdk(stack: ApiStack, user: User, failures: list[str]) -> tuple[SBXClient, _LoseResponse]:
    flaky = _LoseResponse(user.http, failures)
    client = SBXClient("http://testserver", http=flaky, workspace_id=user.workspace_id)
    client.csrf = user.csrf
    return client, flaky


# ---------------------------------------------------------------- AUD20


@pytest.mark.parametrize("failures", [["timeout"], ["502"], ["504"], ["timeout", "503"]])
def test_lost_issuance_response_is_retried_without_a_second_key(stack, failures) -> None:
    """The server commits, the response is lost; the SDK retries with the same key."""
    user = User(stack)
    client, flaky = _sdk(stack, user, failures)
    created = client.create_api_key("ci", scopes=["write"])
    assert len(set(flaky.keys)) == 1 and len(flaky.keys) == len(failures) + 1
    live = _live_keys(stack, user)
    assert len(live) == 1, "one command, one live key"
    assert created["id"] == live[0]["id"] and created["replayed"] is True
    # The plaintext the caller finally received is the live key.
    me = _with_key(stack.client(), "GET", "/api/me", created["key"])
    assert me.status_code == 200 and me.json()["auth"]["scopes"] == ["write"]


def test_same_key_other_body_conflicts_and_mints_nothing(stack) -> None:
    user = User(stack)
    first = user.post("/api/api-keys", {"name": "ci", "scopes": ["read"]}, key="issue-1")
    assert first.status_code == 201
    for body in ({"name": "other", "scopes": ["read"]}, {"name": "ci", "scopes": ["*"]}):
        r = user.post("/api/api-keys", body, key="issue-1")
        assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_conflict"
        assert first.json()["key"] not in r.text
    # The same logical request (scope order and duplicates do not matter) is a replay.
    again = user.post("/api/api-keys", {"name": " ci ", "scopes": ["read", "read"]}, key="issue-1")
    assert again.status_code == 201 and again.json()["key"] == first.json()["key"]
    assert len(_live_keys(stack, user)) == 1


def test_issuance_requires_an_idempotency_key(stack) -> None:
    user = User(stack)
    r = user.http.post("/api/api-keys", json={"name": "x"}, headers={"X-CSRF-Token": user.csrf})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_failed"
    assert _live_keys(stack, user) == []


def test_after_the_replay_window_a_retry_explains_instead_of_minting(stack, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    user = User(stack)
    first = user.post("/api/api-keys", {"name": "ci", "scopes": ["write"]}, key="issue-2").json()
    plaintext = first["key"]
    # The copy is sealed, never plaintext, and only until the window ends.
    row = _live_keys(stack, user)[0]
    assert row["replay_sealed"] and row["replay_until"] - row["created_at"] <= timedelta(
        minutes=10, seconds=1
    )
    assert plaintext not in _at_rest_text(stack)

    stack.db.run(
        lambda u: u.update_where(
            "api_keys", {"id": first["id"]}, {"replay_until": u.now() - timedelta(seconds=1)}
        )
    )
    assert stack.services.identity.purge_issuance_replays() == 1
    assert _live_keys(stack, user)[0]["replay_sealed"] is None
    late = user.post("/api/api-keys", {"name": "ci", "scopes": ["write"]}, key="issue-2")
    assert late.status_code == 409, late.text
    error = late.json()["error"]
    assert error["code"] == "secret_unavailable"
    assert error["details"]["reason"] == "replay_expired"
    assert error["details"]["api_key"]["id"] == first["id"]
    assert "key" not in error["details"]["api_key"] and plaintext not in late.text
    assert len(_live_keys(stack, user)) == 1

    # Recovery: revoke the orphan, then issue a new key with a new request.
    assert user.delete(f"/api/api-keys/{first['id']}").json()["revoked_at"]
    fresh = user.post("/api/api-keys", {"name": "ci", "scopes": ["write"]}, key="issue-3")
    assert fresh.status_code == 201 and fresh.json()["key"] != plaintext
    assert [k["id"] for k in _live_keys(stack, user)] == [fresh.json()["id"]]
    assert plaintext not in caplog.text and fresh.json()["key"] not in caplog.text


def test_revoking_erases_the_replay_copy_and_a_retry_does_not_revive_it(stack) -> None:
    user = User(stack)
    first = user.post("/api/api-keys", {"name": "ci", "scopes": ["read"]}, key="issue-4").json()
    user.delete(f"/api/api-keys/{first['id']}")
    row = stack.db.read(lambda u: u.get("api_keys", first["id"]))
    assert row["replay_sealed"] is None and row["revoked_at"] is not None
    again = user.post("/api/api-keys", {"name": "ci", "scopes": ["read"]}, key="issue-4")
    assert again.status_code == 409
    assert again.json()["error"]["details"]["reason"] == "revoked"
    assert _live_keys(stack, user) == []


def test_keys_are_isolated_per_user(stack) -> None:
    alice, bob = User(stack), User(stack)
    a = alice.post("/api/api-keys", {"name": "ci"}, key="shared-key").json()
    b = bob.post("/api/api-keys", {"name": "ci"}, key="shared-key")
    assert b.status_code == 201 and b.json()["key"] != a["key"]
    assert a["key"] not in b.text


# ---------------------------------------------------------------- AUD21


def test_scope_matrix_and_immediate_revocation(stack) -> None:
    user = User(stack)
    http = stack.client()
    keys = {}
    for scope in ("read", "write", "*"):
        r = user.post("/api/api-keys", {"name": f"k-{scope}", "scopes": [scope]})
        assert r.status_code == 201 and r.json()["scopes"] == [scope]
        keys[scope] = r.json()
    listed = {k["id"]: k for k in user.get("/api/api-keys").json()["items"]}
    assert {listed[k["id"]]["scopes"][0] for k in keys.values()} == {"read", "write", "*"}
    assert all("key" not in k for k in listed.values()), "the list never carries plaintext"

    project = {"slug": "s", "name": "s", "spec": {"repository": None}}
    projects = f"/api/workspaces/{user.workspace_id}/projects"
    read, write, full = (keys[s]["key"] for s in ("read", "write", "*"))
    # read: query yes, write no.
    assert _with_key(http, "GET", projects, read).status_code == 200
    assert _with_key(http, "POST", projects, read, project).status_code == 403
    # write: manage sessions/projects, not keys.
    assert _with_key(http, "POST", projects, write, project).status_code == 201
    assert _with_key(http, "POST", "/api/api-keys", write, {"name": "x"}).status_code == 403
    assert _with_key(http, "DELETE", f"/api/api-keys/{keys['read']['id']}", write).status_code == (
        403
    )
    # full: key management.
    child = _with_key(http, "POST", "/api/api-keys", full, {"name": "child", "scopes": ["read"]})
    assert child.status_code == 201 and child.json()["scopes"] == ["read"]
    revoked = _with_key(http, "DELETE", f"/api/api-keys/{child.json()['id']}", full)
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]
    assert _with_key(http, "GET", "/api/me", child.json()["key"]).status_code == 401

    # Revocation is immediate for reads and writes.
    for scope in ("read", "write"):
        user.delete(f"/api/api-keys/{keys[scope]['id']}")
    for key in (read, write):
        assert _with_key(http, "GET", projects, key).status_code == 401
        r = _with_key(http, "POST", projects, key, {**project, "slug": "after-revoke"})
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"


def test_sdk_create_api_key_passes_scopes(stack) -> None:
    user = User(stack)
    client, _ = _sdk(stack, user, [])
    created = client.create_api_key("reader", scopes=["read"])
    assert created["scopes"] == ["read"] and created["key"].startswith("sbx_key_")
    with pytest.raises(SBXError) as err:
        client.create_api_key("bad", scopes=["admin"])
    assert err.value.code == "validation_failed"
