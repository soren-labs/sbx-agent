"""Runtime evidence PostgreSQL cannot store never wedges ingestion, cancel or release
(issue #228 AUD01). Real runtime + fake OpenCode CLI + embedded PostgreSQL."""

from __future__ import annotations

import json

import pytest
from control.application import ingest as ingest_module
from control.application.ingest import REJECTED
from tests.support.api import ApiStack, User, inference

SPEC = {"defaults": {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}}}
# NUL, a lone surrogate, other C0 controls and an escape sequence in one tool output.
ODD = "before\x00after \ud800 tab\tbell\x07 \x1b[31mred\x1b[0m"


@pytest.fixture
def env(db, tmp_path):
    stack = ApiStack(db, tmp_path)
    user = User(stack)
    user.connect("inference_api", inference("zen-key-unstorable-0000"))
    stack.drain()
    project = user.post(
        f"/api/workspaces/{user.workspace_id}/projects", {"slug": "odd", "spec": SPEC}
    ).json()
    yield stack, user, project
    stack.shutdown()


def start(user: User, project: dict, content: str) -> dict:
    created = user.post(
        f"/api/workspaces/{user.workspace_id}/sessions",
        {"project_id": project["id"], "message": {"content": content}},
    )
    assert created.status_code in (200, 201, 202), created.text
    return created.json()


def turn_state(user: User, turn_id: str) -> str:
    return user.get(f"/api/turns/{turn_id}").json()["turn"]["state"]


def tool_output(user: User, sid: str) -> str | None:
    for m in user.get(f"/api/sessions/{sid}/messages").json()["items"]:
        for p in m["parts"]:
            if p["kind"] == "tool" and (p.get("data") or {}).get("name") == "bash":
                return p["data"].get("output")
    return None


def test_unstorable_tool_output_is_stored_safely_and_the_turn_completes(env) -> None:
    stack, user, project = env
    created = start(user, project, f"odd [tool-output:{json.dumps(ODD)}]")
    stack.drive(lambda: turn_state(user, created["turn_id"]) == "succeeded", timeout=45)
    out = tool_output(user, created["session_id"])
    assert out is not None
    assert "\x00" not in out and "\ud800" not in out
    assert out == ODD.replace("\x00", "�").replace("\ud800", "�")
    events = user.get(f"/api/sessions/{created['session_id']}/events").json()["items"]
    tool = [e for e in events if e["type"] == "tool.completed"]
    assert tool and tool[-1]["payload"]["unrepresentable_replaced"] == 2
    failed = stack.db.read(
        lambda u: [j for j in u.find("jobs", {}) if j["state"] == "failed" or j["attempts"] > 20]
    )
    assert failed == []


def test_cancel_and_release_converge_after_unstorable_output(env) -> None:
    stack, user, project = env
    created = start(user, project, f"odd [tool-output:{json.dumps(ODD)}] [hang]")
    sid, turn_id = created["session_id"], created["turn_id"]
    stack.drive(lambda: tool_output(user, sid) is not None, timeout=45)
    assert user.post(f"/api/turns/{turn_id}/cancellations", {}).status_code in (200, 202)
    stack.drive(lambda: turn_state(user, turn_id) == "cancelled", timeout=45)
    assert user.post(f"/api/sessions/{sid}/executor/releases", {}).status_code in (200, 202)
    stack.drive(
        lambda: user.get(f"/api/sessions/{sid}").json()["session"]["executor"]["lease_id"] is None,
        timeout=45,
    )
    leases = stack.db.read(lambda u: u.find("executor_leases", {"session_id": sid}))
    assert leases and all(lease["state"] == "released" for lease in leases)


@pytest.fixture
def reject(monkeypatch):
    """Make PostgreSQL itself refuse one kind of record while it is applied."""
    real = ingest_module._apply
    kinds: set[str] = set()

    def apply(uow, lease, epoch, seq, item, payload, hooks):
        if item["type"] in kinds:
            uow._one("SELECT 'not a number'::int")  # invalid_text_representation (DataError)
        return real(uow, lease, epoch, seq, item, payload, hooks)

    monkeypatch.setattr(ingest_module, "_apply", apply)
    return kinds


def events_of(user: User, sid: str) -> list[dict]:
    return user.get(f"/api/sessions/{sid}/events").json()["items"]


def test_a_rejected_record_does_not_hold_back_the_evidence_behind_it(env, reject) -> None:
    stack, user, project = env
    reject.add("tool.completed")
    created = start(user, project, "odd [write:a.txt=one]")
    stack.drive(lambda: turn_state(user, created["turn_id"]) == "succeeded", timeout=45)
    rejected = [e for e in events_of(user, created["session_id"]) if e["type"] == REJECTED]
    assert [e["payload"]["rejected_type"] for e in rejected] == ["tool.completed"]
    assert "InvalidTextRepresentation" in rejected[0]["payload"]["error"]
    # The source tuple is settled: later polls and replays never re-raise it.
    jobs = stack.db.read(lambda u: u.find("jobs", {"kind": "execution.reconcile"}))
    assert jobs and all(j["state"] == "succeeded" for j in jobs)


def test_a_rejected_stop_record_still_settles_cancel_and_release(env, reject) -> None:
    stack, user, project = env
    reject.add("execution.stopped")
    created = start(user, project, "odd [hang]")
    sid, turn_id = created["session_id"], created["turn_id"]
    stack.drive(lambda: turn_state(user, turn_id) == "running", timeout=45)
    assert user.post(f"/api/turns/{turn_id}/cancellations", {}).status_code in (200, 202)
    # The stop happened but its record is unusable: the Turn ends without a confirmed stop.
    stack.drive(lambda: turn_state(user, turn_id) in ("cancelled", "interrupted"), timeout=45)
    assert turn_state(user, turn_id) == "interrupted"
    assert user.post(f"/api/sessions/{sid}/executor/releases", {}).status_code in (200, 202)
    stack.drive(
        lambda: user.get(f"/api/sessions/{sid}").json()["session"]["executor"]["lease_id"] is None,
        timeout=45,
    )
