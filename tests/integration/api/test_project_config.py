"""Complete ProjectVersion specs, check argv, declared services and delivery capability (#229)."""

from __future__ import annotations

import copy
import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from control.application.projects import normalize_spec
from control.integrations.git import GitTransport
from tests.support.api import ApiStack, User, inference
from tests.support.git_host import FakeHost, make_repo

VECTORS = json.loads(
    (Path(__file__).parents[2] / "fixtures" / "argv_syntax.json").read_text(encoding="utf-8")
)

# Every section populated, with non-default values and fields this schema does not interpret.
FULL_SPEC: dict[str, Any] = {
    "repository": {"full_name": "acme/demo", "base_ref": "develop", "mirror": "internal"},
    "environment": {"env": {"APP_MODE": "test", "LOG_LEVEL": "debug"}, "setup_hint": "make deps"},
    "checks": [
        {
            "name": "unit",
            "argv": ["python3", "-c", "print('ok')"],
            "timeout_seconds": 120,
            "label": "fast",
        }
    ],
    "services": [
        {
            "name": "web",
            "argv": ["python3", "-m", "http.server", "18950"],
            "cwd": "app",
            "port": 18950,
            "health": {"path": "/healthz"},
            "restart": "never",
            "order": 2,
            "preview": True,
            "owner": "frontend",
        }
    ],
    "defaults": {
        "harness": {"provider_id": "claude", "model": "claude-sonnet", "effort": "high"},
        "executor": {"backend": "local", "resource_class": "large"},
        "turn_defaults": {"max_minutes": 30},
        "budget": {"max_turns": 5},
    },
    "ship_policy": {
        "transport": "git_branch",
        "base_branch": "release",
        "draft": False,
        "automatic_delivery": False,
        "required_results": [
            {"kind": "ReviewAssessment", "count": 2, "verdict": "approve"},
            {"kind": "TestResult", "count": 1},
        ],
        "required_checks": ["ci/unit"],
        "independence": "distinct_child_session",
        "merge_methods": ["merge", "rebase"],
        "require_base_unchanged": True,
    },
    "x_team_notes": {"owner": "platform"},
}


def _paths(value: Any, path: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out.update(_paths(v, f"{path}.{k}" if path else k))
        return out or {path: {}}
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        out = {}
        for i, v in enumerate(value):
            out.update(_paths(v, f"{path}[{i}]"))
        return out
    return {path: value}


def _changed(a: dict[str, Any], b: dict[str, Any]) -> set[str]:
    pa, pb = _paths(a), _paths(b)
    return {k for k in pa.keys() | pb.keys() if pa.get(k, "<absent>") != pb.get(k, "<absent>")}


@pytest.fixture
def stack(db, tmp_path):
    s = ApiStack(db, tmp_path)
    yield s
    s.shutdown()


# ---------------------------------------------------------------- AUD03


def test_full_spec_is_kept_and_a_single_edit_changes_only_that_field(stack) -> None:
    user = User(stack)
    created = user.post(
        f"/api/workspaces/{user.workspace_id}/projects",
        {"slug": "full", "name": "Full", "spec": FULL_SPEC},
    )
    assert created.status_code == 201, created.text
    project = created.json()
    v1 = project["current_version"]["spec"]
    # Nothing given is dropped or replaced by a default.
    assert v1["defaults"]["harness"] == {
        "provider_id": "claude",
        "model": "claude-sonnet",
        "effort": "high",
    }
    assert v1["defaults"]["executor"] == {"backend": "local", "resource_class": "large"}
    assert v1["defaults"]["budget"] == {"max_turns": 5}
    assert v1["environment"] == FULL_SPEC["environment"]
    assert v1["ship_policy"] == FULL_SPEC["ship_policy"]
    assert v1["services"] == FULL_SPEC["services"]
    assert v1["checks"] == FULL_SPEC["checks"]
    assert v1["x_team_notes"] == {"owner": "platform"}
    assert v1["repository"]["mirror"] == "internal"
    assert v1["repository"]["clone_url"] == "https://github.com/acme/demo.git"

    # The Console derives the next version from the stored spec and edits one field.
    edited = copy.deepcopy(v1)
    edited["defaults"]["harness"]["model"] = "claude-opus"
    published = user.post(
        f"/api/projects/{project['id']}/versions",
        {"spec": edited, "expected_version": project["version"]},
    )
    assert published.status_code == 201, published.text
    v2 = published.json()["current_version"]
    assert _changed(v1, v2["spec"]) == {"defaults.harness.model"}

    # The stored form is a fixed point: republishing it keeps every field and the digest.
    again = user.post(
        f"/api/projects/{project['id']}/versions",
        {"spec": v2["spec"], "expected_version": published.json()["version"]},
    ).json()["current_version"]
    assert again["spec"] == v2["spec"] and again["spec_digest"] == v2["spec_digest"]
    assert normalize_spec(normalize_spec(FULL_SPEC)) == normalize_spec(FULL_SPEC)


@pytest.mark.parametrize(
    ("spec", "field"),
    [
        ({"x_team": {"api_token": "abc123"}}, "x_team.api_token"),
        ({"services": [{"name": "w", "argv": ["x"], "password": "hunter2"}]}, "services[0]"),
        ({"checks": [{"name": "c", "argv": ["", "x"]}]}, "checks[0].argv"),
        ({"checks": [{"name": "c", "argv": "python3 -c 1"}]}, "checks[0].argv"),
        (
            {"services": [{"name": "w", "argv": ["a"]}, {"name": "w", "argv": ["b"]}]},
            "services",
        ),
        ({"schema_version": 2}, "schema_version"),
    ],
)
def test_invalid_specs_are_refused_not_altered(stack, spec, field) -> None:
    user = User(stack)
    r = user.post(f"/api/workspaces/{user.workspace_id}/projects", {"slug": "bad", "spec": spec})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_failed"
    assert r.json()["error"]["details"]["field"].startswith(field)
    assert "abc123" not in r.text and "hunter2" not in r.text


def test_empty_arguments_after_the_program_are_kept(stack) -> None:
    user = User(stack)
    spec = {"checks": [{"name": "c", "argv": ["python3", "-c", "import sys; print(sys.argv)", ""]}]}
    r = user.post(f"/api/workspaces/{user.workspace_id}/projects", {"slug": "e", "spec": spec})
    assert r.status_code == 201, r.text
    assert r.json()["current_version"]["spec"]["checks"][0]["argv"][-1] == ""


# ---------------------------------------------------------------- AUD06


def _command(case: dict[str, Any]) -> str:
    line = case["line"]
    prefix = f"{case['name']}="
    return line[len(prefix) :] if line.startswith(prefix) else line


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_vectors_match_posix_shell_words(case) -> None:
    """The Console syntax is shlex (POSIX quoting, no expansion) and round-trips via join."""
    assert shlex.split(_command(case)) == case["argv"]
    assert shlex.split(shlex.join(case["argv"])) == case["argv"]


@pytest.fixture
def repo_env(db, tmp_path):
    bare, url = make_repo(tmp_path)
    host = FakeHost(bare)
    stack = ApiStack(db, tmp_path, git=GitTransport(tmp_path / "git"), host=host)
    user = User(stack)
    user.connect("inference_api", inference("zen-key-project-config-0"))
    stack.drain()
    yield stack, user, host, url
    stack.shutdown()


def _project(user: User, slug: str, spec: dict[str, Any]) -> dict[str, Any]:
    r = user.post(f"/api/workspaces/{user.workspace_id}/projects", {"slug": slug, "spec": spec})
    assert r.status_code == 201, r.text
    return r.json()


def _session(stack, user, body: dict[str, Any], text: str) -> str:
    r = user.post(
        f"/api/workspaces/{user.workspace_id}/sessions", {**body, "message": {"content": text}}
    )
    assert r.status_code in (200, 201, 202), r.text
    created = r.json()
    stack.drive(
        lambda: (
            user.get(f"/api/turns/{created['turn_id']}").json()["turn"]["state"]
            in ("succeeded", "failed")
        ),
        timeout=40,
    )
    return created["session_id"]


def _ready(stack, user, sid: str) -> dict[str, Any]:
    def ready():
        items = user.get(f"/api/sessions/{sid}/changesets").json()["items"]
        return next((c for c in items if c["state"] == "ready"), None)

    stack.drive(lambda: ready() is not None, timeout=40)
    return ready()


def test_declared_checks_run_the_intended_argv(repo_env) -> None:
    """smoke=python3 -c "print(123)" prints 123 through the real check runner."""
    stack, user, _host, url = repo_env
    checks = [{"name": c["name"], "argv": c["argv"]} for c in VECTORS["cases"]]
    project = _project(
        user,
        "checks",
        {
            "repository": {"full_name": "acme/demo", "base_ref": "main"},
            "checks": checks,
            "defaults": {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}},
        },
    )
    sid = _session(
        stack,
        user,
        {
            "project_id": project["id"],
            "repository": {"full_name": "acme/demo", "clone_url": url, "base_ref": "main"},
        },
        "change [write:feature.txt=on]",
    )
    cs = _ready(stack, user, sid)
    result = json.dumps(
        {"kind": "TestResult", "subject_digest": cs["subject_digest"], "status": "pass"}
    )
    spawned = user.post(
        f"/api/sessions/{sid}/delegations",
        {"role": "test", "changeset_id": cs["id"], "instructions": f"[result:{result}]"},
    ).json()
    did = spawned["delegation_id"]
    stack.drive(
        lambda: (
            user.get(f"/api/delegations/{did}").json()["state"]
            in ("succeeded", "failed", "cancelled")
        ),
        timeout=60,
    )
    view = user.get(f"/api/delegations/{did}").json()
    runs = {r["name"]: r for r in view["result"]["evidence"]["platform_checks"]}
    assert set(runs) == {c["name"] for c in VECTORS["cases"]}
    for case in VECTORS["cases"]:
        run = runs[case["name"]]
        assert run["exit_code"] == case["exit_code"], (case["name"], run)
        if "stdout" in case:
            assert run["output_tail"] == case["stdout"], (case["name"], run)
            assert run["status"] == "passed"
        else:
            assert case["output_contains"] in run["output_tail"] and run["status"] == "failed"
    assert runs["smoke"]["output_tail"] == "123\n"
    # A failing assertion is a failing check, whatever the agent claimed.
    assert view["result"]["verdict"] == "fail"


# ---------------------------------------------------------------- AUD15


def _capability(user: User, cs_id: str) -> dict[str, Any]:
    detail = user.get(f"/api/changesets/{cs_id}").json()["delivery"]
    return detail


def test_projectless_changeset_offers_patch_export_not_delivery(repo_env, tmp_path) -> None:
    stack, user, host, _url = repo_env
    sid = _session(
        stack,
        user,
        {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}},
        "draft notes [write:notes.txt=hello]",
    )
    # Without a repository nothing is captured automatically; the user captures explicitly.
    assert user.post(f"/api/sessions/{sid}/changesets", {"origin": "explicit"}).status_code == 202
    cs = _ready(stack, user, sid)
    listed = user.get(f"/api/sessions/{sid}/changesets").json()["items"][0]["delivery"]
    detail = _capability(user, cs["id"])
    assert listed == detail
    assert detail["available"] is False and detail["reason"] == "no_repository"
    assert detail["recovery"] == ["download_patch"] and detail["repository"] is None
    refused = user.post(f"/api/changesets/{cs['id']}/deliveries", {"title": "x"})
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "unsupported_capability"
    assert error["details"]["reason"] == "no_repository"
    assert not host.prs and stack.db.read(lambda u: u.count("deliveries", {"session_id": sid})) == 0

    patch = user.get(f"/api/changesets/{cs['id']}/patch")
    assert patch.status_code == 200
    assert patch.headers["x-subject-digest"] == cs["subject_digest"]
    assert f'filename="{cs["id"]}.patch"' in patch.headers["content-disposition"]
    assert b"notes.txt" in patch.content
    # The export is exactly what was captured: it applies with git apply onto the baseline.
    out = tmp_path / "apply"
    out.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=out, check=True)
    (out / "change.patch").write_bytes(patch.content)
    subprocess.run(["git", "apply", "--binary", "change.patch"], cwd=out, check=True)
    assert (out / "notes.txt").read_text() == "hello\n"
    # Owner-scoped like every other ChangeSet read.
    assert User(stack).get(f"/api/changesets/{cs['id']}/patch").status_code == 404


def test_repository_delivery_capability_matrix(repo_env) -> None:
    stack, user, host, url = repo_env
    repo = {"full_name": "acme/demo", "clone_url": url, "base_ref": "main"}
    base = {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}}

    # No GitHub Connection yet: the form would fail, so the server says why up front.
    sid = _session(stack, user, {**base, "repository": repo}, "x [write:a.txt=1]")
    cs = _ready(stack, user, sid)
    cap = _capability(user, cs["id"])
    assert cap["reason"] == "connection_required" and cap["recovery"] == ["manage_connections"]
    assert cap["repository"] == "acme/demo" and cap["base_branch"] == "main"
    refused = user.post(f"/api/changesets/{cs['id']}/deliveries", {})
    assert refused.json()["error"]["code"] == "connection_required"

    # Adding a GitHub Connection is a real recovery for a Session that pinned none.
    # Dummy value in the shape the validator requires (same pattern as the delivery tests).
    github = user.connect("github", {"token": "ghp_" + "b" * 36})
    stack.drain()
    cap = _capability(user, cs["id"])
    assert cap["available"] is True and cap["connection_id"] == github["id"]
    assert cap["transport"] == "pull_request" and cap["draft"] is True
    delivery = user.post(f"/api/changesets/{cs['id']}/deliveries", {"title": "Add a"}).json()
    did = delivery["delivery"]["id"]
    stack.drive(
        lambda: (
            user.get(f"/api/deliveries/{did}").json()["state"] in ("succeeded", "failed", "blocked")
        ),
        timeout=40,
    )
    assert user.get(f"/api/deliveries/{did}").json()["state"] == "succeeded"
    assert len(host.prs) == 1

    # A Session pinned to a Connection that is later revoked is never silently re-pointed.
    sid2 = _session(stack, user, {**base, "repository": repo}, "y [write:b.txt=2]")
    cs2 = _ready(stack, user, sid2)
    assert _capability(user, cs2["id"])["connection_id"] == github["id"]
    other = user.connect("github", {"token": "ghp_" + "c" * 36}, label="second")
    assert user.delete(f"/api/connections/{github['id']}").json()["state"] == "revoked"
    cap = _capability(user, cs2["id"])
    assert cap["reason"] == "connection_revoked" and cap["connection_id"] is None
    assert other["id"] != github["id"]
    assert (
        user.post(f"/api/changesets/{cs2['id']}/deliveries", {}).json()["error"]["code"]
        == "connection_revoked"
    )

    # A ship policy transport this deployment does not perform: export the patch instead.
    project = _project(
        user,
        "exporting",
        {
            "repository": {"full_name": "acme/demo", "base_ref": "main"},
            "ship_policy": {"transport": "export"},
            "defaults": {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}},
        },
    )
    sid3 = _session(
        stack, user, {"project_id": project["id"], "repository": repo}, "z [write:c.txt=3]"
    )
    cs3 = _ready(stack, user, sid3)
    cap = _capability(user, cs3["id"])
    assert cap["reason"] == "transport_unsupported" and cap["recovery"] == ["download_patch"]

    # A ChangeSet that is not sealed yet cannot be delivered or exported.
    pending = user.post(f"/api/sessions/{sid3}/changesets", {"origin": "explicit"}).json()
    early = user.get(f"/api/changesets/{pending['changeset']['id']}").json()
    assert early["state"] != "ready", "capture runs as a Job; nothing has drained it yet"
    assert early["delivery"]["reason"] == "not_ready" and early["delivery"]["recovery"] == []
    assert user.get(f"/api/changesets/{early['id']}/patch").status_code == 409
    stack.drain()


# ---------------------------------------------------------------- AUD17

SERVICE = {
    "name": "worker",
    "argv": [
        "python3",
        "-u",
        "-c",
        "import time\nprint('svc-229 ready', flush=True)\nwhile True: time.sleep(1)",
    ],
}


def test_declared_service_start_logs_stop_and_its_source(stack) -> None:
    user = User(stack)
    user.connect("inference_api", inference("zen-key-services-229-00"))
    stack.drain()
    project = _project(
        user,
        "svc",
        {
            "services": [SERVICE],
            "defaults": {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}},
        },
    )
    sid = _session(stack, user, {"project_id": project["id"]}, "hi [write:a.txt=1]")
    listed = user.get(f"/api/sessions/{sid}/services").json()
    assert listed["source"]["kind"] == "project_version"
    assert listed["source"]["project_id"] == project["id"]
    assert listed["source"]["project_version_ordinal"] == 1
    assert listed["capabilities"]["preview"]["enabled"] is False
    assert [s["name"] for s in listed["items"]] == ["worker"]
    assert listed["items"][0]["preview_available"] is False

    before = user.get(f"/api/sessions/{sid}/services/worker/logs").json()
    assert before == {"name": "worker", "lines": [], "started": False}
    assert user.get(f"/api/sessions/{sid}/services/undeclared/logs").status_code == 404

    try:
        assert user.post(f"/api/sessions/{sid}/services/worker/activations").status_code == 202
        stack.drain()
        svc = user.get(f"/api/sessions/{sid}/services").json()["items"][0]
        assert svc["desired"] == "running" and svc["state"] in ("ready", "starting")
        lines: list[str] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "svc-229 ready" not in lines:
            logs = user.get(f"/api/sessions/{sid}/services/worker/logs").json()
            lines = logs["lines"]
            time.sleep(0.2)
        assert "svc-229 ready" in lines and logs["started"] is True

        # Desired state outlives the lease: release compute, then the next activation
        # starts the service again on the new lease.
        first_lease = user.get(f"/api/sessions/{sid}").json()["session"]["executor"]["lease_id"]
        user.post(f"/api/sessions/{sid}/executor/releases", {})
        stack.drive(
            lambda: (
                user.get(f"/api/sessions/{sid}").json()["session"]["executor"]["lease_id"] is None
            )
        )
        offline = user.get(f"/api/sessions/{sid}/services").json()["items"][0]
        assert offline["desired"] == "running" and offline["state"] == "offline"
        assert offline["lease_live"] is False
        user.post(f"/api/sessions/{sid}/executor/activations", {})

        def back() -> bool:
            item = user.get(f"/api/sessions/{sid}/services").json()["items"][0]
            return item["lease_live"] and item["state"] in ("ready", "starting")

        stack.drive(back, timeout=40)
        lease = user.get(f"/api/sessions/{sid}").json()["session"]["executor"]["lease_id"]
        assert lease and lease != first_lease
        deadline = time.monotonic() + 10
        lines = []
        while time.monotonic() < deadline and "svc-229 ready" not in lines:
            lines = user.get(f"/api/sessions/{sid}/services/worker/logs").json()["lines"]
            time.sleep(0.2)
        assert "svc-229 ready" in lines
    finally:
        assert user.post(f"/api/sessions/{sid}/services/worker/stops").status_code == 202
        stack.drain()
    svc = user.get(f"/api/sessions/{sid}/services").json()["items"][0]
    assert svc["desired"] == "stopped" and svc["state"] == "stopped"


def test_projectless_session_has_no_declared_services(stack) -> None:
    user = User(stack)
    user.connect("inference_api", inference("zen-key-services-229-01"))
    stack.drain()
    sid = _session(
        stack,
        user,
        {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}},
        "hi",
    )
    listed = user.get(f"/api/sessions/{sid}/services").json()
    assert listed["items"] == [] and listed["source"]["kind"] == "none"
    refused = user.post(f"/api/sessions/{sid}/services/web/activations")
    assert refused.status_code == 404
