"""SOR-80 integration probes: internal DELETE releases /v1 leases; creating
sessions reject messages through the internal API."""

from __future__ import annotations

from datetime import UTC, datetime

from control.api_v1.state import V1State
from control.store import SessionRecord, empty_usage
from fastapi.testclient import TestClient
from tests.integration.control.conftest import AUTH
from tests.integration.control.test_api import wait_session


class _FakeLease:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


def test_internal_delete_releases_v1_lease(client: TestClient, control_env) -> None:
    app, _, _ = control_env
    v1 = V1State()
    app.state.v1_state = v1

    created = client.post("/api/sessions", json={"title": "lease"}, auth=AUTH)
    assert created.status_code == 201
    sid = created.json()["session_id"]
    wait_session(client, sid, status="idle")

    lease = _FakeLease()
    v1.set_lease(sid, lease)
    deleted = client.delete(f"/api/sessions/{sid}", auth=AUTH)
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "closed"
    assert lease.released is True
    assert v1.pop_lease(sid) is None


def test_internal_delete_without_v1_state_still_works(client: TestClient) -> None:
    created = client.post("/api/sessions", json={"title": "plain"}, auth=AUTH)
    sid = created.json()["session_id"]
    wait_session(client, sid, status="idle")
    deleted = client.delete(f"/api/sessions/{sid}", auth=AUTH)
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "closed"


def test_post_message_409_while_creating(client: TestClient, control_env) -> None:
    _, _, store = control_env
    now = datetime.now(UTC)
    store.put(
        SessionRecord(
            id="s-creating",
            title="t",
            status="creating",
            created_at=now,
            updated_at=now,
            model="gpt-5.6-luna",
            turns=0,
            usage=empty_usage(),
            messages=[],
            owner="sbx",
            last_activity_at=now,
        )
    )
    resp = client.post("/api/sessions/s-creating/messages", json={"text": "hi"}, auth=AUTH)
    assert resp.status_code == 409
    assert resp.json()["error"] == "session_not_runnable"
