"""Resident machines through the real composition root and HTTP API (B2/B5).

Real sshd/ssh, agent and runtime; LABELLED FAKE Turn content (deterministic OpenCode
stand-in pinned with OPENCODE_BIN). Checks the discovery routes, workspace scoping, that
no SSH parameter ever leaves the server, and that a retried create is one Session.
"""

from __future__ import annotations

import pytest
from control.executors.resident import ResidentExecutor
from tests.support import sshd as sshd_support
from tests.support.api import ApiStack, User, inference
from tests.support.runtime import FAKE_OPENCODE

pytestmark = pytest.mark.skipif(
    not sshd_support.available(), reason="OpenSSH client/server binaries are not installed"
)


@pytest.fixture
def env(db, tmp_path):
    server = sshd_support.PrivateSshd(tmp_path / "sshd")
    server.start()
    executor = ResidentExecutor(call_timeout=30)
    stack = ApiStack(db, tmp_path, executors={"resident": executor})
    yield stack, server, tmp_path
    for lease in db.read(lambda u: u.find("executor_leases", {})):
        if lease["handle"]:
            machine = db.read(lambda u: u.get("resident_machines", lease["resident_machine_id"]))
            try:
                executor.terminate(lease["handle"], "cleanup", {"target": machine["target"]})
            except Exception:
                pass
    executor.tunnels.close_all()
    server.stop()


def test_discovery_session_and_turn_over_the_api(env) -> None:
    stack, server, tmp = env
    user = User(stack)
    user.connect("inference_api", inference("sk-test-REDACTED-0000000000"))
    stack.drain()
    target = server.target(tmp / "remote", cli_overrides={"OPENCODE_BIN": FAKE_OPENCODE})
    machine = stack.services.resident.register(user.workspace_id, "ubuntu-dev", target)

    listed = user.get(f"/api/workspaces/{user.workspace_id}/resident-machines")
    assert listed.status_code == 200, listed.text
    [item] = listed.json()["items"]
    assert item["id"] == machine["id"] and item["name"] == "ubuntu-dev"
    assert item["reachability"]["status"] == "reachable"
    shown = user.get(f"/api/resident-machines/{machine['id']}?probe=false").json()
    assert shown["reachability"]["status"] == "unchecked"
    backends = {b["kind"] for b in user.get("/api/executor-backends").json()["items"]}
    assert "resident" in backends

    body = {
        "harness": {"provider_id": "opencode"},
        "executor": {"backend": "resident", "resident_machine_id": machine["id"]},
        "message": {"content": "over the api [write:api.txt=ok]"},
    }
    first = user.post(f"/api/workspaces/{user.workspace_id}/sessions", body, key="create-1")
    again = user.post(f"/api/workspaces/{user.workspace_id}/sessions", body, key="create-1")
    assert first.status_code == 202, first.text
    assert again.json()["session_id"] == first.json()["session_id"]
    assert stack.db.read(lambda u: u.count("sessions")) == 1
    session_id, turn_id = first.json()["session_id"], first.json()["turn_id"]
    stack.drive(
        lambda: user.get(f"/api/turns/{turn_id}").json()["turn"]["state"] == "succeeded",
        timeout=90,
    )
    view = user.get(f"/api/sessions/{session_id}").json()["session"]
    assert view["executor"]["backend"] == "resident"
    assert view["executor"]["resident_machine_id"] == machine["id"]
    executor = user.get(f"/api/sessions/{session_id}/executor").json()
    assert executor["resident_machine"]["name"] == "ubuntu-dev"
    assert executor["leases"][0]["image_digest"].startswith("resident:")
    content = user.get(f"/api/sessions/{session_id}/files/content?path=api.txt").json()
    assert content["content"].strip() == "ok"

    stranger = User(stack)
    assert stranger.get(f"/api/resident-machines/{machine['id']}").status_code == 404
    assert stranger.get(f"/api/workspaces/{user.workspace_id}/resident-machines").status_code == 404
    leaked = [
        text
        for text in user.responses + stranger.responses
        for needle in (
            str(server.client_key),
            str(server.known_hosts),
            "identity_file",
            "known_hosts_file",
            "remote_python",
            f'"port": {server.port}',
        )
        if needle in text
    ]
    assert leaked == []


def test_unknown_or_foreign_machine_cannot_be_pinned(env) -> None:
    stack, server, tmp = env
    owner, other = User(stack), User(stack)
    other.connect("inference_api", inference("sk-test-REDACTED-1111111111"))
    stack.drain()
    machine = stack.services.resident.register(
        owner.workspace_id, "mine", server.target(tmp / "remote")
    )
    r = other.post(
        f"/api/workspaces/{other.workspace_id}/sessions",
        {
            "harness": {"provider_id": "opencode"},
            "executor": {"backend": "resident", "resident_machine_id": machine["id"]},
        },
    )
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = other.post(
        f"/api/workspaces/{other.workspace_id}/sessions",
        {
            "harness": {"provider_id": "opencode"},
            "executor": {"backend": "local", "resident_machine_id": machine["id"]},
        },
    )
    assert r.status_code == 422
