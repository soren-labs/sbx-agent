import os

from control.api.app import create_app
from control.composition import assemble
from control.security.vault import EnvelopeVault
from control.storage.local import LocalObjects
from fastapi.testclient import TestClient


def resources(database, tmp_path):
    key_file = tmp_path / "test-master"
    if not key_file.exists():
        key_file.write_bytes(os.urandom(32))
    master = key_file.read_bytes()

    class GitHub:
        def client(self, credential):
            raise AssertionError("a pure request must not contact GitHub")

    return assemble(
        database,
        EnvelopeVault({"1": master}),
        LocalObjects(tmp_path / "objects"),
        master,
        lambda *args: (_ for _ in ()).throw(AssertionError("read launched compute")),
        {"github": GitHub()},
    )


def login(client, email="one@example.test"):
    response = client.post(
        "/api/auth/login",
        json={"email": email, "password": "REDACTED"},
        headers={"Idempotency-Key": "login"},
    )
    assert response.status_code == 200
    return {"Idempotency-Key": "create", "X-CSRF-Token": client.cookies["sbx_csrf"]}


def test_unified_api_atomic_composer_csrf_pure_reads_restart_and_isolation(database, tmp_path):
    r = resources(database, tmp_path)
    user = r.identity.register("one@example.test", "REDACTED", verified=True)
    client = TestClient(create_app(r, secure_cookies=False))
    headers = login(client)
    wid = user["workspace_id"]
    response = client.post(
        "/api/workspaces/" + wid + "/sessions",
        json={"backend": "local", "message": {"content": "accepted"}},
        headers=headers,
    )
    assert response.status_code == 201
    accepted = response.json()
    assert accepted["message_id"] and accepted["turn_id"] and accepted["job_id"]
    assert (
        client.post(
            "/api/workspaces/" + wid + "/sessions",
            json={"backend": "local", "message": {"content": "accepted"}},
            headers=headers,
        ).json()
        == accepted
    )
    sid = accepted["session_id"]
    before = client.get("/api/sessions/" + sid).json()
    assert before["turns"][0]["state"] == "queued"
    for _ in range(3):
        assert client.get("/api/sessions/" + sid).json() == before
        assert client.get("/api/sessions/" + sid + "/executor").json() == {"lease": None}
    csrf = client.post(
        "/api/sessions/" + sid + "/messages",
        json={"content": "forbidden"},
        headers={"Idempotency-Key": "bad"},
    )
    assert csrf.status_code == 403
    restarted = TestClient(create_app(resources(database, tmp_path), secure_cookies=False))
    login(restarted)
    assert restarted.get("/api/sessions/" + sid).json() == before
    other = r.identity.register("two@example.test", "REDACTED", verified=True)
    outsider = TestClient(create_app(r, secure_cookies=False))
    login(outsider, "two@example.test")
    for path in [
        "/api/sessions/" + sid,
        "/api/jobs/" + accepted["job_id"],
        "/api/workspaces/" + wid + "/sessions",
    ]:
        assert outsider.get(path).status_code in {403, 404}
    assert (
        outsider.get("/api/workspaces/" + other["workspace_id"] + "/sessions").json()["items"] == []
    )
    events = client.get("/api/sessions/" + sid + "/events").json()
    assert events["event_watermark"] == before["event_watermark"]
    assert all(e["seq"] > 0 for e in events["events"])


def test_write_only_credentials_no_validation_echo_and_no_legacy_routes(database, tmp_path):
    r = resources(database, tmp_path)
    user = r.identity.register("one@example.test", "REDACTED", verified=True)
    client = TestClient(create_app(r, secure_cookies=False))
    headers = login(client)
    response = client.post(
        "/api/workspaces/" + user["workspace_id"] + "/connections",
        json={"kind": "opencode_zen", "credential": {"api_key": "REDACTED"}},
        headers=headers,
    )
    assert response.status_code == 202
    assert "REDACTED" not in response.text
    cid = response.json()["connection_id"]
    assert "credential" not in client.get("/api/connections/" + cid).text
    assert client.get("/api/models", params={"connection_id": cid}).json()["models"] == []
    bad = client.post(
        "/api/workspaces/" + user["workspace_id"] + "/connections",
        json={"kind": "unsupported", "credential": {"api_key": "REDACTED"}},
        headers=headers,
    )
    assert bad.status_code == 422 and "REDACTED" not in bad.text
    routes = client.get("/openapi.json").json()["paths"]
    assert all(not p.startswith(("/v1", "/v2", "/hosted", "/api/v2")) for p in routes)


def test_api_key_scope_and_password_change_revoke_all_login_authority(database, tmp_path):
    r = resources(database, tmp_path)
    user = r.identity.register("one@example.test", "REDACTED", verified=True)
    client = TestClient(create_app(r, secure_cookies=False))
    headers = login(client)
    issued = client.post(
        "/api/api-keys",
        json={"workspace_id": user["workspace_id"], "scopes": ["read"]},
        headers=headers,
    )
    assert issued.status_code == 201
    bearer = {"Authorization": "Bearer " + issued.json()["key"]}
    assert client.get("/api/me", headers=bearer).status_code == 200
    assert (
        client.post(
            "/api/workspaces/" + user["workspace_id"] + "/sessions",
            json={},
            headers={**bearer, "Idempotency-Key": "denied"},
        ).status_code
        == 403
    )
    replay = client.post(
        "/api/api-keys",
        json={"workspace_id": user["workspace_id"], "scopes": ["read"]},
        headers=headers,
    )
    assert replay.json()["id"] == issued.json()["id"] and "key" not in replay.json()
    changed = client.post(
        "/api/auth/password-changes",
        json={"current_password": "REDACTED", "password": "REDACTED"},
        headers={**headers, "Idempotency-Key": "password"},
    )
    assert changed.status_code == 200
    assert client.get("/api/me").status_code == 403
    assert client.get("/api/me", headers=bearer).status_code == 403
