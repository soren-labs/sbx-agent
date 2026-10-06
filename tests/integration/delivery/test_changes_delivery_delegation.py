"""ChangeSet capture, generic Delegation, exact-subject Delivery and merge gate (A20-A26)."""

from __future__ import annotations

import json
import time

import pytest
from control.domain.errors import DomainError
from control.integrations.git import GitTransport
from protocol.manifests import canonical_manifest, subject_digest
from tests.support.api import ApiStack, User
from tests.support.git_host import FakeHost, git, make_repo

CHECK = {
    "name": "feature-present",
    "argv": [
        "python3",
        "-c",
        "import pathlib,sys; sys.exit(0 if pathlib.Path('feature.txt').exists() else 1)",
    ],
}


@pytest.fixture
def env(db, tmp_path):
    bare, url = make_repo(tmp_path)
    host = FakeHost(bare)
    stack = ApiStack(db, tmp_path, git=GitTransport(tmp_path / "git"), host=host)
    user = User(stack)
    user.connect("opencode_zen", {"api_key": "zen-key-delivery-0000"})
    user.connect("github", {"token": "ghp_" + "a" * 36})
    stack.drain()
    yield stack, user, host, bare, url
    stack.shutdown()


def _session(stack, user, url, text, *, project_id=None):
    body = {
        "project_id": project_id,
        "harness": {"provider_id": "opencode"},
        "executor": {"backend": "local"},
        "repository": {"full_name": "acme/demo", "clone_url": url, "base_ref": "main"},
        "checks": [CHECK],
        "message": {"content": text},
    }
    created = user.post(f"/api/workspaces/{user.workspace_id}/sessions", body).json()
    return created["session_id"], created["turn_id"]


def _ready_changeset(stack, user, sid):
    def ready():
        items = user.get(f"/api/sessions/{sid}/changesets").json()["items"]
        return next((c for c in items if c["state"] == "ready"), None)

    stack.drive(lambda: ready() is not None, timeout=40)
    return ready()


def _delivered(stack, user, did):
    done = ("succeeded", "failed", "blocked", "cancelled")
    stack.drive(lambda: user.get(f"/api/deliveries/{did}").json()["state"] in done, timeout=40)
    view = user.get(f"/api/deliveries/{did}").json()
    assert view["state"] == "succeeded", (view["state_reason"], view["steps"])
    return view


def _delegation(stack, user, did, state="succeeded"):
    stack.drive(
        lambda: (
            user.get(f"/api/delegations/{did}").json()["state"]
            in ("succeeded", "failed", "cancelled")
        ),
        timeout=40,
    )
    view = user.get(f"/api/delegations/{did}").json()
    assert view["state"] == state, view
    return view


def test_capture_review_test_deliver_and_merge(env) -> None:
    stack, user, host, bare, url = env
    sid, tid = _session(stack, user, url, "build it [write:feature.txt=done]")
    cs = _ready_changeset(stack, user, sid)
    assert cs["origin"] == "automatic" and cs["automatic_eligible"] is True
    detail = user.get(f"/api/changesets/{cs['id']}").json()
    assert [f["path"] for f in detail["files"]] == ["feature.txt"]
    rebuilt = canonical_manifest(
        repository="acme/demo",
        base_sha=detail["base_sha"],
        baseline_tree=detail["baseline_tree"],
        files=[
            {"path": f["path"], "type": f["type"], "mode": f["mode"], "digest": f["digest"]}
            for f in detail["files"]
        ],
        tree_sha=detail["tree_sha"],
    )
    assert subject_digest(rebuilt) == cs["subject_digest"]
    assert "feature.txt" in user.get(f"/api/changesets/{cs['id']}/diff").json()["diff"]
    assert (
        user.get(f"/api/changesets/{cs['id']}/files?path=feature.txt").json()["content"] == "done\n"
    )
    digest = cs["subject_digest"]

    review_result = json.dumps(
        {"kind": "ReviewAssessment", "subject_digest": digest, "verdict": "approve", "findings": []}
    )
    spawned = user.post(
        f"/api/sessions/{sid}/delegations",
        {
            "role": "review",
            "changeset_id": cs["id"],
            "context": "Please review the feature.",
            "instructions": f"[result:{review_result}]",
        },
    ).json()
    wait = user.post(
        f"/api/delegations/{spawned['delegation_id']}/waits", {"wake": "message"}
    ).json()
    assert wait["state"] == "pending"
    review = _delegation(stack, user, spawned["delegation_id"])
    assert review["result"]["verdict"] == "approve" and review["result"]["independent"] is True
    assert (
        review["result"]["subject_digest"] == digest
        and next(i for i in review["inputs"] if i["kind"] == "changeset")["applied"] is True
    )
    child = user.get(f"/api/sessions/{spawned['child_session_id']}").json()["session"]
    assert child["parent_session_id"] == sid and child["role"] == "review"
    child_lease = stack.db.read(
        lambda u: u.find_one(
            "executor_leases", {"session_id": spawned["child_session_id"], "state": "ready"}
        )
    )
    assert (
        stack.services.execution.connector.channel(child_lease).query(
            "files.read", path="feature.txt"
        )["content"]
        == "done\n"
    )
    assert (
        stack.db.read(lambda u: u.get("wait_subscriptions", wait["subscription_id"]))["state"]
        == "satisfied"
    )
    wake = [
        m
        for m in user.get(f"/api/sessions/{sid}/messages").json()["items"]
        if m["author_kind"] == "session"
    ]
    assert wake and "verdict=approve" in wake[0]["content"][0]["text"]

    test_result = json.dumps(
        {"kind": "TestResult", "subject_digest": digest, "status": "pass", "checks": []}
    )
    tested = user.post(
        f"/api/sessions/{sid}/delegations",
        {"role": "test", "changeset_id": cs["id"], "instructions": f"[result:{test_result}]"},
    ).json()
    test_view = _delegation(stack, user, tested["delegation_id"])
    assert test_view["result"]["verdict"] == "pass"
    assert test_view["result"]["evidence"]["platform_checks"][0]["status"] == "passed"

    delivery = user.post(f"/api/changesets/{cs['id']}/deliveries", {"title": "Add feature"}).json()[
        "delivery"
    ]
    _delivered(stack, user, delivery["id"])
    d = user.get(f"/api/deliveries/{delivery['id']}").json()
    assert d["pull_request"]["draft"] is True and len(host.prs) == 1
    remote = git(bare, "rev-parse", f"refs/heads/{d['target_ref']}")
    assert (
        remote == d["commit_sha"] and git(bare, "rev-parse", f"{remote}^{{tree}}") == cs["tree_sha"]
    )
    assert [s["kind"] for s in d["steps"]] == ["materialize", "push", "pull_request"]
    again = user.post(f"/api/changesets/{cs['id']}/deliveries", {"title": "Add feature"}).json()[
        "delivery"
    ]
    _delivered(stack, user, again["id"])
    again_view = user.get(f"/api/deliveries/{again['id']}").json()
    assert again_view["commit_sha"] == d["commit_sha"] and len(host.prs) == 1
    assert again_view["steps"][1]["evidence"]["already_present"] is True

    pins = {
        "expected_head_sha": d["commit_sha"],
        "subject_digest": digest,
        "expected_version": d["version"],
        "method": "squash",
    }
    user.post(f"/api/deliveries/{d['id']}/merge-requests", pins)
    stack.drain()
    blocked = user.get(f"/api/deliveries/{d['id']}").json()["merge_requests"][0]
    assert blocked["state"] == "blocked" and "pull_request_is_draft" in blocked["gate"]["reasons"]
    d = user.get(f"/api/deliveries/{d['id']}").json()
    user.post(
        f"/api/deliveries/{d['id']}/merge-requests",
        {**pins, "expected_version": d["version"], "mark_ready": True},
    )
    stack.drain()
    merged = user.get(f"/api/deliveries/{d['id']}").json()
    assert merged["merge_requests"][0]["state"] == "succeeded", merged["merge_requests"][0]
    assert host.merges == [{"number": 1, "sha": d["commit_sha"], "method": "squash"}]
    assert git(bare, "rev-parse", "refs/heads/main") == d["commit_sha"]
    types = [e["type"] for e in user.get(f"/api/sessions/{sid}/events").json()["items"]]
    for t in (
        "changeset.ready",
        "delegation.created",
        "delegation.result_published",
        "delivery.succeeded",
        "delivery.merge_requested",
        "delivery.merged",
    ):
        assert t in types


def test_gate_blocks_missing_or_stale_results_and_remote_drift(env) -> None:
    stack, user, host, bare, url = env
    sid, _ = _session(stack, user, url, "v1 [write:feature.txt=one]")
    cs1 = _ready_changeset(stack, user, sid)
    review = json.dumps(
        {
            "kind": "ReviewAssessment",
            "subject_digest": cs1["subject_digest"],
            "verdict": "approve",
            "findings": [],
        }
    )
    did = user.post(
        f"/api/sessions/{sid}/delegations",
        {"role": "review", "changeset_id": cs1["id"], "instructions": f"[result:{review}]"},
    ).json()["delegation_id"]
    _delegation(stack, user, did)
    t2 = user.post(
        f"/api/sessions/{sid}/messages", {"content": "v2 [write:feature.txt=two]"}
    ).json()["turn_id"]
    stack.drive(lambda: user.get(f"/api/turns/{t2}").json()["turn"]["state"] == "succeeded")
    stack.drive(
        lambda: (
            len(
                [
                    c
                    for c in user.get(f"/api/sessions/{sid}/changesets").json()["items"]
                    if c["state"] == "ready"
                ]
            )
            == 2
        ),
        timeout=40,
    )
    cs2 = next(
        c
        for c in user.get(f"/api/sessions/{sid}/changesets").json()["items"]
        if c["source_turn_id"] == t2
    )
    assert cs2["subject_digest"] != cs1["subject_digest"]
    d = user.post(f"/api/changesets/{cs2['id']}/deliveries", {}).json()["delivery"]
    _delivered(stack, user, d["id"])
    d = user.get(f"/api/deliveries/{d['id']}").json()
    user.post(
        f"/api/deliveries/{d['id']}/merge-requests",
        {
            "expected_head_sha": d["commit_sha"],
            "subject_digest": cs2["subject_digest"],
            "expected_version": d["version"],
            "mark_ready": True,
        },
    )
    stack.drain()
    gate = user.get(f"/api/deliveries/{d['id']}").json()["merge_requests"][0]["gate"]
    assert "missing_required_result:ReviewAssessment" in gate["reasons"], (
        "approval of v1 does not authorize v2"
    )
    work = bare.parent / "intruder"
    git(bare.parent, "clone", "-q", str(bare), str(work))
    git(work, "checkout", "-q", d["target_ref"])
    (work / "extra.txt").write_text("x\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "intrude")
    git(work, "push", "-q", "origin", d["target_ref"])
    review2 = json.dumps(
        {
            "kind": "ReviewAssessment",
            "subject_digest": cs2["subject_digest"],
            "verdict": "approve",
            "findings": [],
        }
    )
    did2 = user.post(
        f"/api/sessions/{sid}/delegations",
        {"role": "review", "changeset_id": cs2["id"], "instructions": f"[result:{review2}]"},
    ).json()["delegation_id"]
    _delegation(stack, user, did2)
    d = user.get(f"/api/deliveries/{d['id']}").json()
    user.post(
        f"/api/deliveries/{d['id']}/merge-requests",
        {
            "expected_head_sha": d["commit_sha"],
            "subject_digest": cs2["subject_digest"],
            "expected_version": d["version"],
            "mark_ready": True,
        },
    )
    stack.drain()
    gate = user.get(f"/api/deliveries/{d['id']}").json()["merge_requests"][0]["gate"]
    assert "remote_head_changed" in gate["reasons"] and host.merges == []


def test_invalid_results_never_approve_and_cancel_salvage_rules(env) -> None:
    stack, user, host, bare, url = env
    sid, _ = _session(stack, user, url, "base [write:feature.txt=x]")
    cs = _ready_changeset(stack, user, sid)
    bad = user.post(
        f"/api/sessions/{sid}/delegations",
        {
            "role": "review",
            "changeset_id": cs["id"],
            "instructions": "reply without a result block",
        },
    ).json()
    failed = _delegation(stack, user, bad["delegation_id"], "failed")
    assert failed["state_reason"] == "output_contract_invalid" and failed["result"] is None
    wrong = json.dumps(
        {
            "kind": "ReviewAssessment",
            "subject_digest": "sha256:" + "0" * 64,
            "verdict": "approve",
            "findings": [],
        }
    )
    pinned = user.post(
        f"/api/sessions/{sid}/delegations",
        {"role": "review", "changeset_id": cs["id"], "instructions": f"[result:{wrong}]"},
    ).json()
    assert (
        _delegation(stack, user, pinned["delegation_id"], "failed")["state_reason"]
        == "output_contract_invalid"
    )
    assert (
        user.post(
            f"/api/delegations/{pinned['delegation_id']}/result", {"verdict": "approve"}
        ).status_code
        == 403
    )
    tid = user.post(
        f"/api/sessions/{sid}/messages", {"content": "partial [write:half.txt=h] [hang]"}
    ).json()["turn_id"]
    stack.drive(lambda: user.get(f"/api/turns/{tid}").json()["turn"]["state"] == "running")
    time.sleep(0.5)
    user.post(f"/api/turns/{tid}/cancellations", {})
    stack.drive(lambda: user.get(f"/api/turns/{tid}").json()["turn"]["state"] == "cancelled")
    salvage = user.post(
        f"/api/sessions/{sid}/changesets", {"origin": "explicit", "source_turn_id": tid}
    ).json()["changeset"]
    assert salvage["origin"] == "salvage"
    stack.drive(
        lambda: user.get(f"/api/changesets/{salvage['id']}").json()["state"] == "ready", timeout=40
    )
    view = user.get(f"/api/changesets/{salvage['id']}").json()
    assert view["automatic_eligible"] is False and "half.txt" in [f["path"] for f in view["files"]]
    with pytest.raises(DomainError) as err:
        stack.db.run(
            lambda u: stack.services.deliveries.create_in(
                u, "application", u.get("changesets", salvage["id"]), {}, authorization="automatic"
            )
        )
    assert err.value.code == "gate_blocked"


def test_parent_close_cancels_children_and_tool_gateway_scopes(env) -> None:
    stack, user, host, bare, url = env
    sid, _ = _session(stack, user, url, "seed [write:feature.txt=x]")
    cs = _ready_changeset(stack, user, sid)
    session = stack.db.read(lambda u: u.get("sessions", sid))
    token = stack.db.run(lambda u: stack.services.tools.mint(u, session))
    http = stack.client()

    def tool(name, args, op="op-1"):
        return http.post(
            f"/internal/tools/{name}",
            json={"operation_id": op, "args": args},
            headers={"Authorization": f"SBX-Tool {token}"},
        )

    first = tool(
        "sbx.sessions.spawn",
        {"role": "research", "changeset_id": cs["id"], "instructions": "dig [hang]"},
    ).json()
    again = tool(
        "sbx.sessions.spawn",
        {"role": "research", "changeset_id": cs["id"], "instructions": "dig [hang]"},
    ).json()
    assert first["delegation_id"] == again["delegation_id"], "tool calls dedupe by operation id"
    assert tool("sbx.deliveries.request", {"changeset_id": cs["id"]}).status_code == 403
    assert tool("sbx.sessions.read", {"session_id": first["child_session_id"]}).status_code == 200
    other = User(stack)
    other.connect("opencode_zen", {"api_key": "zen-key-other-0000"})
    stack.drain()
    stranger = other.post(
        f"/api/workspaces/{other.workspace_id}/sessions",
        {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}},
    ).json()
    assert tool("sbx.sessions.read", {"session_id": stranger["session_id"]}).status_code == 404
    assert (
        http.post(
            "/internal/tools/sbx.sessions.read",
            json={"operation_id": "x", "args": {}},
            headers={"Authorization": "SBX-Tool nope"},
        ).status_code
        == 401
    )
    stack.drive(
        lambda: stack.db.read(lambda u: u.get("turns", first["turn_id"]))["state"] == "running",
        timeout=40,
    )
    user.post(f"/api/sessions/{sid}/closures", {})
    stack.drain()
    view = user.get(f"/api/delegations/{first['delegation_id']}").json()
    assert view["state"] == "cancelled" and view["child"]["lifecycle"] == "closed"
    assert tool("sbx.sessions.read", {}, op="op-9").status_code == 401, (
        "grant dies with the Session"
    )


@pytest.mark.parametrize(
    "tightening,reason",
    [
        ({"require_base_unchanged": True}, "unsupported_base_stability"),
        ({"merge_methods": ["merge"]}, "merge_method_not_allowed"),
        ({"merge_methods": []}, "merge_method_not_allowed"),
        ({"required_checks": ["ship"]}, "check_not_passing:ship"),
        (
            {"required_results": [{"kind": "ReviewAssessment", "count": 1, "verdict": "approve"}]},
            "missing_required_result:ReviewAssessment",
        ),
    ],
)
def test_old_delivery_obeys_current_mandatory_policy(env, tightening, reason) -> None:
    stack, user, host, _, url = env
    permissive = {"required_results": [], "merge_methods": ["squash", "merge"]}
    spec = {"ship_policy": permissive}
    project = user.post(
        f"/api/workspaces/{user.workspace_id}/projects",
        {"slug": "policy", "spec": spec},
    ).json()
    sid, _ = _session(stack, user, url, "build [write:feature.txt=done]", project_id=project["id"])
    cs = _ready_changeset(stack, user, sid)
    d = user.post(f"/api/changesets/{cs['id']}/deliveries", {}).json()["delivery"]
    d = _delivered(stack, user, d["id"])
    pinned = stack.db.read(lambda u: u.get("deliveries", d["id"]))["policy"]
    assert pinned["required_results"] == [] and not pinned["require_base_unchanged"]
    tightened = {"ship_policy": {**permissive, **tightening}}
    updated = user.post(
        f"/api/projects/{project['id']}/versions",
        {"spec": tightened, "expected_version": project["version"]},
    )
    assert updated.status_code == 201, updated.text
    pins = {
        "expected_head_sha": d["commit_sha"],
        "subject_digest": cs["subject_digest"],
        "expected_version": d["version"],
        "method": "squash",
        "mark_ready": True,
    }
    response = user.post(f"/api/deliveries/{d['id']}/merge-requests", pins)
    assert response.status_code == 202, response.text
    stack.drain()
    view = user.get(f"/api/deliveries/{d['id']}").json()
    blocked = view["merge_requests"][0]
    assert blocked["state"] == "blocked" and reason in blocked["gate"]["reasons"]
    assert host.merges == [], "policy tightened after creation must still block the old Delivery"
    assert stack.db.read(lambda u: u.get("deliveries", d["id"]))["policy"] == pinned

    if "required_checks" in tightening:
        host.check_runs[d["commit_sha"]] = [{"name": "ship", "conclusion": "success"}]
    elif "required_results" in tightening:
        review = json.dumps(
            {
                "kind": "ReviewAssessment",
                "subject_digest": cs["subject_digest"],
                "verdict": "approve",
                "findings": [],
            }
        )
        delegation = user.post(
            f"/api/sessions/{sid}/delegations",
            {"role": "review", "changeset_id": cs["id"], "instructions": f"[result:{review}]"},
        ).json()
        _delegation(stack, user, delegation["delegation_id"])
    elif tightening.get("merge_methods"):
        pins["method"] = "merge"
    else:
        # Base stability is unsupported: only removing that current requirement
        # can permit merging. An empty method allowlist similarly permits none.
        user.post(
            f"/api/projects/{project['id']}/versions",
            {"spec": spec, "expected_version": updated.json()["version"]},
        ).raise_for_status()
    pins["expected_version"] = user.get(f"/api/deliveries/{d['id']}").json()["version"]
    response = user.post(f"/api/deliveries/{d['id']}/merge-requests", pins)
    assert response.status_code == 202, response.text
    stack.drain()
    merged = user.get(f"/api/deliveries/{d['id']}").json()["merge_requests"][0]
    assert merged["state"] == "succeeded", merged
    assert host.merges == [{"number": 1, "sha": d["commit_sha"], "method": pins["method"]}]
