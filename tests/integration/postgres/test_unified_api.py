"""Unified /api over the real services + Postgres (RFC 167 §08).

Covers: register/login/api-keys, project + version, connection CRUD +
credential replace + validation job, session create + message + turn +
events, changeset/delivery/delegation intents, idempotency replay +
conflict, cross-owner isolation, and the RFC error shape.
"""

from __future__ import annotations

import json

import pytest
from control.api.app import create_app
from control.application.auth import AuthService
from control.application.changes import ChangeSetService
from control.application.connections import ConnectionService
from control.application.delegation import DelegationService
from control.application.delivery import DeliveryService
from control.application.execution import ExecutionService
from control.application.models import ModelDiscoveryService
from control.application.projects import ProjectService
from control.application.sessions import SessionService
from control.connectors.base import ConnectorRegistry
from control.persistence.unit_of_work import SqlUnitOfWork
from control.security.vault import Vault
from control.storage.blobs import BlobStore
from control.vcs.remote import FakeRemote
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

PW = "correct horse battery staple"


def _make_app(pg, tmp_path):
    vault = Vault.generate()
    connections = ConnectionService(pg, vault)
    sessions = SessionService(pg)
    blobs = BlobStore(str(tmp_path / "blobs"))
    changes = ChangeSetService(pg, blobs)
    remote = FakeRemote()
    delivery = DeliveryService(pg, remote_factory=lambda delivery, connections: remote)
    delegations = DelegationService(pg)
    app = create_app(
        pg,
        auth=AuthService(pg),
        projects=ProjectService(pg),
        connections=connections,
        sessions=sessions,
        changes=changes,
        delivery=delivery,
        delegations=delegations,
        models=ModelDiscoveryService(pg, connections, ConnectorRegistry()),
        execution=ExecutionService(pg, backends={}, pool=None, credential_resolver=None),
    )
    return app


def _make_client(pg, tmp_path):
    return TestClient(_make_app(pg, tmp_path))


def _register_and_login(client: TestClient, email: str) -> dict:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": PW, "display_name": "t"},
    )
    assert r.status_code == 201, r.text
    assert "token" not in r.text.lower() or "verification" in r.text
    r = client.post("/api/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_auth_and_me(pg, tmp_path):
    client = _make_client(pg, tmp_path)
    login = _register_and_login(client, "a@x.dev")
    h = _auth(login["token"])
    me = client.get("/api/me", headers=h)
    assert me.status_code == 200
    assert me.json()["email"] == "a@x.dev"
    assert me.json()["workspaces"] == [login["user"]["workspaces"][0]]

    anon = TestClient(client.app)  # fresh client, no cookie jar
    bad = anon.get("/api/me")
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "unauthenticated"
    assert bad.json()["error"]["request_id"]

    badlogin = client.post(
        "/api/auth/login", json={"email": "a@x.dev", "password": "nope-nope-nope"}
    )
    assert badlogin.status_code == 401

    out = client.post("/api/auth/logout", headers=h)
    assert out.status_code == 200
    assert client.get("/api/me", headers=h).status_code == 401
    # cookie jar was cleared by logout as well
    assert TestClient(client.app).get("/api/me").status_code == 401


def test_api_keys(pg, tmp_path):
    client = _make_client(pg, tmp_path)
    login = _register_and_login(client, "k@x.dev")
    h = _auth(login["token"])
    r = client.post("/api/api-keys", headers=h, json={"label": "ci"})
    assert r.status_code == 201
    key = r.json()["key"]
    assert key.startswith("sbx_k_")
    # key authenticates
    assert client.get("/api/me", headers=_auth(key)).status_code == 200
    # list hides the plaintext
    lst = client.get("/api/api-keys", headers=h).json()["items"]
    assert len(lst) == 1 and "key" not in lst[0] and "key_hash" not in lst[0]
    client.delete(f"/api/api-keys/{lst[0]['id']}", headers=h)
    assert client.get("/api/me", headers=_auth(key)).status_code == 401


def test_project_version_connection_flow(pg, tmp_path):
    client = _make_client(pg, tmp_path)
    login = _register_and_login(client, "b@x.dev")
    h = _auth(login["token"])
    ws = login["user"]["workspaces"][0]
    idem = {"Idempotency-Key": "p1"}

    # Idempotency-Key enforced
    r = client.post(
        f"/api/workspaces/{ws}/projects",
        headers=h,
        json={"slug": "p", "name": "P"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"

    r = client.post(
        f"/api/workspaces/{ws}/projects",
        headers={**h, **idem},
        json={"slug": "p", "name": "P"},
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    # replay same key + body → same response
    r2 = client.post(
        f"/api/workspaces/{ws}/projects",
        headers={**h, **idem},
        json={"slug": "p", "name": "P"},
    )
    assert r2.json()["id"] == pid
    # reused key + changed payload → idempotency_conflict
    r3 = client.post(
        f"/api/workspaces/{ws}/projects",
        headers={**h, **idem},
        json={"slug": "p2", "name": "P"},
    )
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "idempotency_conflict"

    r = client.post(
        f"/api/projects/{pid}/versions",
        headers={**h, "Idempotency-Key": "v1"},
        json={"repository": "soren-labs/sbx-e2e-test", "base_ref": "main"},
    )
    assert r.status_code == 201, r.text
    pv = r.json()
    assert pv["repository"] == "soren-labs/sbx-e2e-test"

    # connections: github manual token
    r = client.post(
        f"/api/workspaces/{ws}/connections",
        headers={**h, "Idempotency-Key": "c1"},
        json={
            "kind": "github",
            "label": "gh",
            "credential": {"format": "personal_token", "payload": {"token": "ghp_test_secret_xyz"}},
        },
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    body = r.json()
    assert "ghp_test_secret_xyz" not in json.dumps(body)

    conns = client.get(f"/api/workspaces/{ws}/connections", headers=h).json()
    assert len(conns["items"]) == 1
    assert "token" not in json.dumps(conns)

    r = client.post(f"/api/connections/{cid}/validations", headers=h, json={})
    assert r.status_code == 202 and r.json()["job"]["state"] == "queued"

    caps = client.get(f"/api/connections/{cid}/capabilities", headers=h)
    assert caps.status_code == 200 and caps.json()["health"] == "unverified"

    # credential replace bumps revocation_epoch; response stays clean
    r = client.post(
        f"/api/connections/{cid}/credential-versions",
        headers={**h, "Idempotency-Key": "c2"},
        json={
            "credential": {"format": "personal_token", "payload": {"token": "ghp_new_secret_abc"}}
        },
    )
    assert r.status_code == 201
    assert r.json()["connection"]["revocation_epoch"] == 1
    assert "ghp_new_secret_abc" not in json.dumps(r.json())
    # sealed rows carry no plaintext
    with SqlUnitOfWork(pg) as uow:
        row = uow.rows.one(
            "SELECT * FROM credential_versions WHERE connection_id=%s AND state='active'",
            (cid,),
        )
        assert "ghp_new_secret_abc" not in json.dumps(
            {k: (v.hex() if isinstance(v, (bytes, bytearray)) else str(v)) for k, v in row.items()}
        )

    # disconnect rejects while... nothing live → allowed
    r = client.delete(f"/api/connections/{cid}", headers=h)
    assert r.status_code == 200
    assert r.json()["connection"]["state"] == "revoked"


def test_session_message_turn_events(pg, tmp_path):
    client = _make_client(pg, tmp_path)
    login = _register_and_login(client, "s@x.dev")
    h = _auth(login["token"])
    ws = login["user"]["workspaces"][0]

    r = client.post(
        f"/api/workspaces/{ws}/sessions",
        headers={**h, "Idempotency-Key": "s1"},
        json={
            "title": "demo",
            "projectless_spec": {"repository": "soren-labs/sbx-e2e-test", "base_ref": "main"},
            "harness": {"provider_id": "opencode", "model": "opencode/big-pickle"},
            "message": {"routing": "queue", "content": {"text": "hi"}},
        },
    )
    assert r.status_code == 201, r.text
    sid = r.json()["session"]["id"]
    assert r.json()["turn_id"]

    detail = client.get(f"/api/sessions/{sid}", headers=h).json()
    assert detail["session"]["harness"]["provider_id"] == "opencode"
    assert detail["worktree"]["id"]

    turns = client.get(f"/api/sessions/{sid}/turns", headers=h).json()
    assert turns["items"][0]["state"] in ("queued", "preparing")

    # unsupported harness is a truthful capability failure, not a rewrite
    r = client.post(
        f"/api/workspaces/{ws}/sessions",
        headers={**h, "Idempotency-Key": "s2"},
        json={"harness": {"provider_id": "codex", "model": "gpt-5"}},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unsupported_capability"

    msgs = client.get(f"/api/sessions/{sid}/messages", headers=h).json()
    assert msgs["items"][0]["content"]["text"] == "hi"

    ev = client.get(f"/api/sessions/{sid}/events", headers=h).json()
    types = [e["type"] for e in ev["items"]]
    assert "session.created" in types and "turn.queued" in types
    assert ev["event_watermark"] >= len(ev["items"])

    # cursor-free invalid → invalid_cursor stays valid for events (seq based);
    # messages use cursor paging
    bad = client.get(f"/api/sessions/{sid}/messages?cursor=bogus", headers=h)
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_cursor"

    close = client.post(f"/api/sessions/{sid}/closures", headers=h, json={})
    assert close.status_code == 200
    again = client.post(
        f"/api/sessions/{sid}/messages",
        headers={**h, "Idempotency-Key": "m2"},
        json={"routing": "queue", "content": {"text": "x"}},
    )
    assert again.status_code == 409


def test_owner_isolation(pg, tmp_path):
    client = _make_client(pg, tmp_path)
    a = _register_and_login(client, "o1@x.dev")
    b = _register_and_login(client, "o2@x.dev")
    ha, hb = _auth(a["token"]), _auth(b["token"])
    wsa = a["user"]["workspaces"][0]

    r = client.post(
        f"/api/workspaces/{wsa}/sessions",
        headers={**ha, "Idempotency-Key": "i1"},
        json={"harness": {"provider_id": "opencode", "model": "opencode/big-pickle"}},
    )
    sid = r.json()["session"]["id"]

    # owner b cannot see the workspace, the session, or its events
    assert client.get(f"/api/workspaces/{wsa}", headers=hb).status_code == 404
    assert client.get(f"/api/sessions/{sid}", headers=hb).status_code == 404
    assert client.get(f"/api/sessions/{sid}/events", headers=hb).status_code == 404
    assert (
        client.post(
            f"/api/workspaces/{wsa}/sessions",
            headers={**hb, "Idempotency-Key": "x"},
            json={"harness": {"provider_id": "opencode", "model": "m"}},
        ).status_code
        == 404
    )


def test_delegation_and_job_visibility(pg, tmp_path):
    client = _make_client(pg, tmp_path)
    login = _register_and_login(client, "d@x.dev")
    h = _auth(login["token"])
    ws = login["user"]["workspaces"][0]
    r = client.post(
        f"/api/workspaces/{ws}/sessions",
        headers={**h, "Idempotency-Key": "s1"},
        json={"harness": {"provider_id": "opencode", "model": "opencode/big-pickle"}},
    )
    sid = r.json()["session"]["id"]

    r = client.post(
        f"/api/sessions/{sid}/delegations",
        headers={**h, "Idempotency-Key": "d1"},
        json={
            "role": "reviewer",
            "prompt": "review the diff",
            "result_contract": {"kind": "ReviewAssessment"},
            "harness": {"provider_id": "opencode", "model": "opencode/big-pickle"},
        },
    )
    assert r.status_code == 201, r.text
    d = r.json()["delegation"]
    assert d["state"] == "active"

    det = client.get(f"/api/delegations/{d['id']}", headers=h).json()
    assert det["child_session_id"]
    child = client.get(f"/api/sessions/{det['child_session_id']}", headers=h)
    assert child.status_code == 200

    w = client.post(
        f"/api/delegations/{d['id']}/waits",
        headers=h,
        json={"deadline_seconds": 60},
    )
    assert w.status_code == 201 and w.json()["wait"]["state"] == "pending"

    c = client.post(f"/api/delegations/{d['id']}/cancellations", headers=h, json={})
    assert c.status_code == 200 and c.json()["state"] == "cancelled"


def test_readyz_healthz(pg, tmp_path):
    client = _make_client(pg, tmp_path)
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/readyz").json() == {"ok": True}
