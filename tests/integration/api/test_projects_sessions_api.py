"""Projects/versions, Session pinning and a full API Turn with a manual Zen Connection."""

from __future__ import annotations

import pytest
from tests.support.api import ApiStack, User, inference

ZEN = "zen-api-key-for-turns-0001"


@pytest.fixture
def stack(db, tmp_path):
    s = ApiStack(db, tmp_path)
    yield s
    s.shutdown()


SPEC = {
    "repository": None,
    "checks": [{"name": "unit", "argv": ["python3", "-c", "print('ok')"]}],
    "defaults": {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}},
}


def test_project_versions_are_immutable_and_sessions_pin(stack) -> None:
    user = User(stack)
    user.connect("inference_api", inference(ZEN))
    stack.drain()
    created = user.post(
        f"/api/workspaces/{user.workspace_id}/projects",
        {"slug": "demo", "name": "Demo", "spec": SPEC},
    )
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["current_version"]["ordinal"] == 1
    bad = user.post(
        f"/api/workspaces/{user.workspace_id}/projects",
        {"slug": "env", "spec": {**SPEC, "environment": {"env": {"API_TOKEN": "x"}}}},
    )
    assert bad.status_code == 422
    session = user.post(
        f"/api/workspaces/{user.workspace_id}/sessions", {"project_id": project["id"]}
    ).json()["session"]
    assert session["project_version_id"] == project["current_version"]["id"]
    assert session["harness"]["model"] == "test-model", "defaults to the preferred free model"
    conflict = user.post(
        f"/api/projects/{project['id']}/versions", {"spec": SPEC, "expected_version": 99}
    )
    assert conflict.json()["error"]["code"] == "version_conflict"
    v2 = user.post(
        f"/api/projects/{project['id']}/versions",
        {"spec": {**SPEC, "checks": []}, "expected_version": 1},
    ).json()
    assert v2["current_version"]["ordinal"] == 2
    pinned = user.get(f"/api/sessions/{session['id']}").json()["session"]
    assert pinned["project_version_id"] == project["current_version"]["id"]
    assert len(user.get(f"/api/projects/{project['id']}/versions").json()["items"]) == 2


def test_api_turn_with_manual_zen_connection_and_replayable_events(stack) -> None:
    user = User(stack)
    zen = user.connect("inference_api", inference(ZEN))
    stack.drain()
    body = {
        "harness": {"provider_id": "opencode"},
        "executor": {"backend": "local"},
        "message": {"content": "hello api [write:x.txt=1]"},
    }
    first = user.post(f"/api/workspaces/{user.workspace_id}/sessions", body, key="create-1")
    assert first.status_code == 202
    again = user.post(f"/api/workspaces/{user.workspace_id}/sessions", body, key="create-1")
    assert again.json()["turn_id"] == first.json()["turn_id"]
    sid, tid = first.json()["session_id"], first.json()["turn_id"]
    stack.drive(lambda: user.get(f"/api/turns/{tid}").json()["turn"]["state"] == "succeeded")
    turn = user.get(f"/api/turns/{tid}").json()["turn"]
    assert turn["executions"][0]["state"] == "succeeded" and turn["evidence_complete"]
    messages = user.get(f"/api/sessions/{sid}/messages").json()["items"]
    assert (
        messages[-1]["role"] == "assistant"
        and "ACK: hello api" in messages[-1]["parts"][-1]["content"]
    )
    page = user.get(f"/api/sessions/{sid}/events?after=0&limit=5").json()
    assert [e["seq"] for e in page["items"]] == [1, 2, 3, 4, 5]
    rest = user.get(f"/api/sessions/{sid}/events?after={page['next_after']}").json()
    assert rest["items"][0]["seq"] == 6 and rest["items"][-1]["seq"] == rest["event_watermark"]
    conflict = user.get(f"/api/sessions/{sid}/events?after=3", headers={"Last-Event-ID": "4"})
    assert conflict.json()["error"]["code"] == "invalid_cursor"
    with user.http.stream(
        "GET",
        f"/api/sessions/{sid}/events?after=0&max_seconds=1",
        headers={"Accept": "text/event-stream"},
    ) as stream:
        text = "".join(stream.iter_text())
    assert "id: 1\nevent: session.created" in text
    cred = user.get(f"/api/connections/{zen['id']}").json()["credential"]["id"]
    execution = stack.db.read(lambda u: u.find_one("executions", {"turn_id": tid}))
    assert (
        execution["credential_version_id"] == cred
        and execution["inference_connection_id"] == zen["id"]
    )
    assert ZEN not in user.all_text()
    assert ZEN not in str(stack.db.read(lambda u: u.find("session_events", {"session_id": sid})))
