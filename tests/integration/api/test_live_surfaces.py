"""Files, terminal, services and operations over the live lease; never waking compute on GET."""

from __future__ import annotations

import time

import pytest
from tests.support.api import ApiStack, User

SPEC = {
    "defaults": {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}},
    "services": [
        {
            "name": "web",
            "argv": ["python3", "-m", "http.server", "18931", "--bind", "127.0.0.1"],
            "port": 18931,
            "health": {"path": "/"},
        }
    ],
}


@pytest.fixture
def env(db, tmp_path):
    stack = ApiStack(db, tmp_path)
    user = User(stack)
    user.connect("opencode_zen", {"api_key": "zen-key-live-000000"})
    stack.drain()
    project = user.post(
        f"/api/workspaces/{user.workspace_id}/projects", {"slug": "live", "spec": SPEC}
    ).json()
    created = user.post(
        f"/api/workspaces/{user.workspace_id}/sessions",
        {"project_id": project["id"], "message": {"content": "hi [write:a.txt=one]"}},
    ).json()
    stack.drive(
        lambda: user.get(f"/api/turns/{created['turn_id']}").json()["turn"]["state"] == "succeeded"
    )
    yield stack, user, created["session_id"]
    for name in ("web",):
        user.post(f"/api/sessions/{created['session_id']}/services/{name}/stops", {})
    stack.drain()
    stack.shutdown()


def test_files_read_write_with_content_precondition(env) -> None:
    stack, user, sid = env
    listing = user.get(f"/api/sessions/{sid}/files").json()["items"]
    assert "a.txt" in [i["path"] for i in listing]
    current = user.get(f"/api/sessions/{sid}/files/content?path=a.txt").json()
    assert current["content"] == "one\n"
    stale = user.http.put(
        f"/api/sessions/{sid}/files",
        json={"path": "a.txt", "content": "two\n", "expected_digest": "sha256:" + "0" * 64},
        headers={"X-CSRF-Token": user.csrf, "Idempotency-Key": "w1"},
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    ok = user.http.put(
        f"/api/sessions/{sid}/files",
        json={"path": "a.txt", "content": "two\n", "expected_digest": current["digest"]},
        headers={"X-CSRF-Token": user.csrf, "Idempotency-Key": "w2"},
    )
    assert ok.status_code == 200
    assert user.get(f"/api/sessions/{sid}/files/content?path=a.txt").json()["content"] == "two\n"
    escape = user.get(f"/api/sessions/{sid}/files/content?path=../state/journal.sqlite")
    assert escape.status_code in (404, 409, 422)
    types = [e["type"] for e in user.get(f"/api/sessions/{sid}/events").json()["items"]]
    assert "worktree.changed" in types


def test_terminal_roundtrip_and_services_lifecycle(env) -> None:
    stack, user, sid = env
    term = user.post(f"/api/sessions/{sid}/terminals", {}).json()["terminal_id"]
    user.post(f"/api/sessions/{sid}/terminals/{term}/input", {"data": "echo sbx-term-$((40+2))\n"})
    out = ""
    for _ in range(50):
        out = user.get(f"/api/sessions/{sid}/terminals/{term}/output?after=0").json()["data"]
        if "sbx-term-42" in out:
            break
        time.sleep(0.1)
    assert "sbx-term-42" in out
    assert user.get(f"/api/sessions/{sid}/services").json()["items"][0]["desired"] == "stopped"
    accepted = user.post(f"/api/sessions/{sid}/services/web/activations", {})
    assert accepted.status_code == 202
    stack.drain()
    svc = user.get(f"/api/sessions/{sid}/services").json()["items"][0]
    assert svc["desired"] == "running" and svc["state"] in ("ready", "starting")
    assert user.get(f"/api/jobs/{accepted.json()['job_id']}").json()["state"] == "succeeded"
    assert "lines" in user.get(f"/api/sessions/{sid}/services/web/logs").json()
    assert (
        user.post(f"/api/sessions/{sid}/services/web/preview-grants", {}).json()["error"]["code"]
        == "unsupported_capability"
    )
    other = User(stack)
    assert other.get(f"/api/jobs/{accepted.json()['job_id']}").status_code == 404


def test_live_reads_report_unavailable_after_release(env) -> None:
    stack, user, sid = env
    user.post(f"/api/sessions/{sid}/executor/releases", {})
    stack.drive(
        lambda: user.get(f"/api/sessions/{sid}").json()["session"]["executor"]["lease_id"] is None
    )
    leases = stack.db.read(lambda u: u.count("executor_leases", {"session_id": sid}))
    for path in (f"/api/sessions/{sid}/files", f"/api/sessions/{sid}/changes"):
        r = user.get(path)
        assert r.status_code == 409 and r.json()["error"]["code"] == "executor_unavailable"
    assert user.get(f"/api/sessions/{sid}/services").json()["items"][0]["state"] == "offline"
    assert stack.db.read(lambda u: u.count("executor_leases", {"session_id": sid})) == leases, (
        "GET never wakes compute"
    )
