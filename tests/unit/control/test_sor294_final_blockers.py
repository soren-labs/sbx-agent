"""Final independent-review blockers: real-provider limits and owner delivery."""

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from control.app import create_app
from control.auth_store import AuthDatabase, AuthStore, PersistentApiKeyStore
from control.backend import SandboxSpec
from control.hosted_auth import HostedAuthError
from control.modal_connection import ModalContext
from control.real_modal import RealModalProvider
from fastapi.testclient import TestClient
from runtime.runner.effort import CANONICAL_EFFORTS
from tests.unit.api_v2.conftest import create_session, wait_session
from tests.unit.test_hosted_workflow import workflow_app as workflow_app


@pytest.mark.parametrize("effort", CANONICAL_EFFORTS)
@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("owner", ["alice", "bob"])
def test_final001_real_sdk_boundary_limits_tags_with_execution_and_recovery(effort, restore, owner):
    calls = []
    tags = {
        "session_id": "agent",
        "owner": owner,
        "provider": "codex",
        "account_id": "account",
        "hosted": "1",
        "modal_connection": "connection-" + owner,
        "modal_workspace": "workspace-" + owner,
        "runtime_image": "image",
        "compute": '{"cpu":2,"memory_mib":2048}',
        "resources": '{"secrets":["reference"]}',
        "reasoning_effort": effort,
        "effort_surface": ",".join(CANONICAL_EFFORTS),
        "recovering": "true",
        "lifecycle_claim": "claim",
    }

    def create(*args, **kwargs):
        assert len(kwargs["tags"]) <= 10, "real Modal tag limit exceeded"
        calls.append(kwargs)
        return SimpleNamespace(object_id="sandbox")

    sdk = SimpleNamespace(
        Client=SimpleNamespace(from_credentials=lambda *a: object()),
        App=SimpleNamespace(lookup=lambda *a, **kw: object()),
        Image=SimpleNamespace(from_id=lambda *a, **kw: object()),
        Sandbox=SimpleNamespace(create=create),
    )
    provider = RealModalProvider(sdk=sdk)
    context = ModalContext(
        owner, "connection-" + owner, {"token_id": "REDACTED", "token_secret": "REDACTED"}
    )
    operation = provider.restore if restore else provider.create
    handle = operation(context, SandboxSpec(tags=tags, cpu=2, memory_mib=2048), {"image": "image"})
    assert all(
        calls[0]["tags"][key] == tags[key]
        for key in ("owner", "session_id", "modal_connection", "modal_workspace", "runtime_image")
    )
    assert handle.tags["reasoning_effort"] == effort
    assert calls[0]["cpu"] == 2 and calls[0]["memory"] == 2048
    assert not set(calls[0]["tags"]) & {
        "compute",
        "resources",
        "reasoning_effort",
        "effort_surface",
        "recovering",
        "lifecycle_claim",
    }


@pytest.fixture
def uncommitted_codex(tmp_path, monkeypatch):
    source = Path("tests/fakes/hosted_codex.py").read_text()
    start = source.index('    subprocess.run(["git", "add"')
    end = source.index("    emit(", start)
    executable = tmp_path / "uncommitted_codex.py"
    executable.write_text(source[:start] + source[end:])
    executable.chmod(0o755)
    monkeypatch.setenv("CODEX_BIN", str(executable))


def auto_session(client, headers, repo, branch="final-auto"):
    return create_session(
        client,
        headers,
        repository={"repo": repo, "ref": "main"},
        delivery={"branch": branch, "pull_request": {"draft": True}, "auto_publish": True},
    )["session"]["id"]


def delivered(client, headers, sid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        delivery = client.get(f"/v2/sessions/{sid}", headers=headers).json()["session"]["delivery"]
        if delivery["status"] in {"delivered", "failed"}:
            return delivery
        time.sleep(0.05)
    pytest.fail("automatic delivery never settled")


def test_final002_auto_publish_owner_app_captures_uncommitted_work_without_ambient(
    workflow_app, uncommitted_codex, monkeypatch
):
    app, owner, headers, repo = workflow_app
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "SBX_GITHUB_EPHEMERAL"):
        monkeypatch.delenv(key, raising=False)
    with TestClient(app, base_url="https://testserver") as client:
        sid = auto_session(client, headers, repo)
        wait_session(client, headers, sid, "finished", "failed")
        delivery = delivered(client, headers, sid)
        assert delivery["status"] == "delivered", delivery
        assert delivery["pull_request"]["draft"]
        assert (
            delivery["pushed_head_sha"]
            != app.state.workspaces.get(app.state.task_store.get(sid).agent_id).base_sha
        )
        bare = app.state.github_connections.for_user(owner.id).mock_repo(repo)
        assert (
            subprocess.check_output(
                ["git", "--git-dir", str(bare), "show", "final-auto:hello.txt"], text=True
            ).strip()
            == "hello placeholder"
        )
        workspace = app.state.workspaces.get(app.state.task_store.get(sid).agent_id)
        assert workspace.dirty and workspace.head_sha == workspace.base_sha
        original = delivery
        app.state.auto_delivery.reconcile()
        assert delivered(client, headers, sid) == original
        replay = client.post(f"/v2/sessions/{sid}/deliver", headers=headers, json={})
        assert replay.status_code == 200
        assert (
            replay.json()["revision"]["delivery"]["pushed_head_sha"] == original["pushed_head_sha"]
        )
        # A subsequent explicit user edit supersedes automatic defaults.
        ready = client.post(
            f"/v2/sessions/{sid}/deliver",
            headers=headers,
            json={"pull_request": {"draft": False}},
        )
        assert ready.status_code == 200
        assert not ready.json()["revision"]["delivery"]["pull_request"]["draft"]
        app.state.auto_delivery.reconcile()
        assert not delivered(client, headers, sid)["pull_request"]["draft"]


@pytest.mark.parametrize("effort", CANONICAL_EFFORTS)
def test_final001_hosted_create_restore_and_reattach_keep_durable_metadata(
    workflow_app, monkeypatch, effort
):
    app, owner, _, _ = workflow_app
    backend = app.state.plane.backend.source
    provider = app.state.compute_provider
    for name in ("create", "restore"):
        original = getattr(provider, name)

        def bounded(context, spec, runtime, original=original):
            assert len(spec.tags) <= 10
            assert "reasoning_effort" not in spec.tags and "recovering" not in spec.tags
            return original(context, spec, runtime)

        monkeypatch.setattr(provider, name, bounded)
    tags = {
        "session_id": "manual-agent",
        "owner": owner.id,
        "provider": "codex",
        "account_id": "account",
        "compute": '{"cpu":2,"memory_mib":2048}',
        "resources": '{"secrets":["reference"]}',
        "reasoning_effort": effort,
        "effort_surface": ",".join(CANONICAL_EFFORTS),
        "recovering": "true",
        "lifecycle_claim": "claim",
    }
    handle = backend.create(SandboxSpec(tags=tags, cpu=2, memory_mib=2048))
    restored = None
    try:
        snapshot = backend.snapshot(handle)
        restored = backend.restore(snapshot, SandboxSpec(tags=tags, cpu=2, memory_mib=2048))
        assert restored.tags["reasoning_effort"] == effort
        assert backend.list({"owner": owner.id, "reasoning_effort": effort})
        other = app.state.auth_store.create_user(email="tag-isolation@example.test")
        app.state.connections.connect(
            other.id, "modal", {"token_id": "REDACTED", "token_secret": "REDACTED"}
        )
        app.state.modal_connections.provision(other.id)
        with pytest.raises(HostedAuthError, match="snapshot_not_found"):
            backend.restore(snapshot, SandboxSpec(tags={**tags, "owner": other.id}))
        with pytest.raises(HostedAuthError, match="sandbox_not_found"):
            backend.poll(
                __import__("dataclasses").replace(
                    restored, tags={**restored.tags, "owner": other.id}
                )
            )
        assert backend.list({"owner": other.id}) == []
        assert (
            backend.records.get("hosted_sandboxes", restored.id, owner=owner.id)["tags"]["compute"]
            == tags["compute"]
        )
    finally:
        backend.terminate(handle)
        if restored:
            backend.terminate(restored)


def test_final002_reconstruction_recovers_crash_after_upstream_pr_before_record(
    workflow_app, uncommitted_codex, monkeypatch
):
    app, owner, headers, repo = workflow_app

    # Crash-style BaseException bypasses normal failure persistence after the
    # real fake-upstream push/PR, just before committing delivered state.
    class WorkerExit(BaseException):
        pass

    put = app.state.revision_store.put_revision

    def crash_after_upstream(revision):
        if (revision.delivery or {}).get("status") == "delivered":
            raise WorkerExit()
        return put(revision)

    monkeypatch.setattr(app.state.revision_store, "put_revision", crash_after_upstream)
    auto = app.state.auto_delivery.deliver

    def interrupted(agent):
        try:
            auto(agent)
        except WorkerExit:
            pass

    app.state.plane.auto_delivery_hook = interrupted
    with TestClient(app, base_url="https://testserver") as client:
        sid = auto_session(client, headers, repo, "final-restart")
        agent = app.state.task_store.get(sid).agent_id
        until = time.monotonic() + 10
        while time.monotonic() < until:
            upstream = (
                app.state.github_connections.for_user(owner.id)
                .remote(repo)
                .find_pull(repo, "final-restart")
            )
            if upstream and app.state.run_store.get(agent, 1).status == "FINISHED":
                break
            time.sleep(0.05)
        assert upstream and app.state.revisions.latest(agent).delivery is None
    restored = create_app(
        backend=app.state.compute_provider.source,
        auth_store=AuthStore(AuthDatabase(path=app.state.auth_store.database._path)),
        hosted=True,
        state_backend="postgres",
        connection_vault=app.state.connections.vault,
        runner_cmd=[sys.executable, "-m", "runtime.runner"],
    )
    with TestClient(restored, base_url="https://testserver") as client:
        delivery = delivered(client, headers, sid)
        assert delivery["status"] == "delivered"
        assert delivery["pull_request"]["number"] == upstream["number"]
        assert delivery["pushed_head_sha"] == upstream["head"]["sha"]
        for _ in range(2):
            restored.state.auto_delivery.reconcile()
        assert delivered(client, headers, sid) == delivery
        assert len(restored.state.github_connections.for_user(owner.id).remote(repo)._pulls()) == 1


def test_final002_two_owner_installations_and_foreign_repo_fail_closed(
    workflow_app, uncommitted_codex, monkeypatch
):
    app, owner, headers, repo = workflow_app
    other = app.state.auth_store.create_user(email="auto-other@example.test")
    app.state.connections.connect(
        other.id, "modal", {"token_id": "REDACTED", "token_secret": "REDACTED"}
    )
    app.state.modal_connections.provision(other.id)
    authorization = app.state.codex_broker.authorize(other.id)
    app.state.codex_broker.callback(other.id, authorization["state"], f"mock:{other.id}")
    github = app.state.github_connections.for_user(other.id)
    github.complete_authorization(
        github._client.installation_id, github.begin_authorization()["state"]
    )
    other_repo = github._client.repositories[0]
    other_headers = {
        "Authorization": "Bearer "
        + PersistentApiKeyStore(app.state.auth_store).create(user_id=other.id)[1]
    }
    original = app.state.github_connections.for_user
    services = {owner.id: original(owner.id), other.id: original(other.id)}
    monkeypatch.setattr(app.state.github_connections, "for_user", lambda user: services[user])
    with TestClient(app, base_url="https://testserver") as client:
        for user, auth, repository in [(owner, headers, repo), (other, other_headers, other_repo)]:
            sid = auto_session(client, auth, repository, "same-branch")
            wait_session(client, auth, sid, "finished", "failed")
            result = delivered(client, auth, sid)
            assert result["status"] == "delivered"
            assert repository in result["pull_request"]["url"]
            foreign_auth = other_headers if user == owner else headers
            assert (
                client.post(
                    f"/v2/sessions/{sid}/deliver", headers=foreign_auth, json={}
                ).status_code
                == 404
            )
        assert (
            services[owner.id]._client.installation_id != services[other.id]._client.installation_id
        )
        for user in (owner, other):
            assert services[user.id]._client.mints
            assert all(
                m["installation_id"] == services[user.id]._client.installation_id
                and m["repositories"] == ["alpha"]
                for m in services[user.id]._client.mints
            )
        # Even a corrupted repo declaration cannot borrow the other owner's
        # authorized installation during background reconstruction.
        agent = app.state.task_store.get(sid).agent_id
        revision = app.state.revisions.latest(agent)
        revision.repo = "https://github.com/" + repo
        revision.delivery = None
        app.state.revision_store.put_revision(revision)
        foreign_before = len(services[owner.id]._client.mints)
        app.state.auto_delivery.deliver(agent)
        assert app.state.revisions.latest(agent).delivery["status"] == "failed"
        assert len(services[owner.id]._client.mints) == foreign_before
