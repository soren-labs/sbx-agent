"""Confirmed hosted-alpha review defects, credential-free reproductions."""

import io
import subprocess
import tarfile
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from control.backend import SandboxSpec
from control.codex_process import AccessOnlyProcess
from control.hosted_auth import HostedAuthError
from control.hosted_lifecycle import HostedLifecycle
from control.modal_connection import ModalConnectionService
from control.sandbox_io import drain
from deploy.hosted import rollout
from fastapi.testclient import TestClient
from runtime.runner.access_scope import AUTH_PATHS, access_scope, cleanup
from tests.unit.api_v2.conftest import create_session, wait_session
from tests.unit.test_hosted_workflow import workflow_app as workflow_app


@pytest.mark.parametrize("failure", ["stdout", "wait", "cancel", "close"])
def test_rev003_operation_cleans_on_transport_wait_cancel_and_close(failure):
    cleaned, killed = [], []

    def stream():
        yield "event"
        if failure == "stdout":
            raise RuntimeError("transport failure")

    def wait():
        if failure == "wait":
            raise RuntimeError("wait failure")
        return 0

    scoped = AccessOnlyProcess(
        SimpleNamespace(stdout=stream(), wait=wait, kill=lambda: killed.append(True)),
        lambda: cleaned.append(True),
    )
    if failure == "cancel":
        scoped.kill()
    elif failure == "close":
        next(scoped.stdout)
        scoped.stdout.close()
    else:
        with pytest.raises(RuntimeError):
            drain(scoped)
    assert cleaned == [True]
    assert killed


@pytest.mark.parametrize("failed", [False, True])
def test_rev003_sandbox_finally_and_stale_cleanup_cannot_delete_new_lease(
    tmp_path, monkeypatch, failed
):
    monkeypatch.setenv("SBX_WORK", str(tmp_path))
    monkeypatch.setenv("SBX_NATIVE_OPERATION", "new-operation")
    with pytest.raises(RuntimeError) if failed else __import__("contextlib").nullcontext():
        with access_scope():
            for relative in AUTH_PATHS:
                path = tmp_path / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("REDACTED")
            assert not cleanup(tmp_path, "old-operation")
            assert all((tmp_path / p).exists() for p in AUTH_PATHS)
            if failed:
                raise RuntimeError("operation failure")
    assert all(not (tmp_path / p).exists() for p in AUTH_PATHS)
    (tmp_path / ".native-access-operation").write_text("new-operation")
    (tmp_path / "auth.json").write_text("REDACTED")
    assert cleanup(tmp_path, "old-operation")
    assert (tmp_path / "auth.json").exists()
    cleanup(tmp_path, "new-operation")
    assert not (tmp_path / "auth.json").exists()


def test_rev001_sweep_claim_retains_concurrent_followup_and_restart(workflow_app, monkeypatch):
    app, owner, headers, repo = workflow_app
    with TestClient(app, base_url="https://testserver") as client:
        sid = create_session(client, headers, repository={"repo": repo, "ref": "main"})["session"][
            "id"
        ]
        wait_session(client, headers, sid, "finished")
        agent = app.state.task_store.get(sid).agent_id
        rec = idle(app, agent)
        rec.last_activity_at = datetime.now(UTC) - timedelta(hours=1)
        app.state.plane.store.put(rec)
        entered, release = threading.Event(), threading.Event()
        provider = app.state.compute_provider
        original = provider.snapshot

        def capture(*args):
            entered.set()
            assert release.wait(10)
            return original(*args)

        monkeypatch.setattr(provider, "snapshot", capture)
        worker = threading.Thread(target=HostedLifecycle(app).sweep)
        worker.start()
        try:
            assert entered.wait(5)
            assert app.state.plane.get(agent).sandbox_tags.get("lifecycle_claim")
            response = client.post(
                f"/v2/sessions/{sid}/messages",
                headers=headers,
                json={"prompt": "Fix during checkpoint"},
            )
            assert response.status_code == 202
            assert app.state.run_store.get(agent, 2).status == "QUEUED"
        finally:
            release.set()
            worker.join(timeout=10)
        assert app.state.plane.get(agent).status == "suspended"
        assert any(
            m.get("text") == "Fix during checkpoint" for m in app.state.plane.get(agent).messages
        )
        app.state.plane.reconcile_turns()
        until = time.monotonic() + 15
        while not app.state.run_store.get(agent, 2).terminal and time.monotonic() < until:
            time.sleep(0.05)
        assert app.state.run_store.get(agent, 2).status == "FINISHED"
        rec = idle(app, agent)
        rec.status = "creating"
        rec.sandbox_tags["lifecycle_claim"] = "interrupted-before-release"
        app.state.plane.store.put(rec)
        result = HostedLifecycle(app).sweep()
        assert result["action_kinds"]["suspended"] == 1
        assert (
            app.state.hosted_accounts.scoped(owner.id).running_count(rec.sandbox_tags["account_id"])
            == 0
        )


def idle(app, agent_id):
    until = time.monotonic() + 10
    while time.monotonic() < until:
        rec = app.state.plane.get(agent_id)
        if rec.status == "idle" and not rec.current_turn_id:
            return rec
        time.sleep(0.02)
    pytest.fail("agent did not settle idle")


@pytest.mark.parametrize(
    "status,bound", [("creating", False), ("creating", True), ("running", True), ("idle", True)]
)
def test_rev001_create_after_sweep_listing_is_not_orphan_killed(monkeypatch, status, bound):
    from control.backend import LocalProcessBackend
    from control.reaper import reap
    from control.store import InMemoryStore, SessionRecord

    backend, store = LocalProcessBackend(), InMemoryStore()
    now = datetime.now(UTC)
    handles = []

    def publish_after_record_listing():
        handle = backend.create(SandboxSpec(tags={"session_id": "late", "owner": "owner"}))
        handles.append(handle)
        store.put(
            SessionRecord(
                id="late",
                title="late",
                status=status,
                created_at=now,
                updated_at=now,
                model="gpt-6.1-sol",
                turns=0,
                usage=None,
                messages=[],
                owner="owner",
                sandbox_id=handle.id if bound else None,
                sandbox_root=str(handle.root) if bound else None,
                sandbox_tags=handle.tags,
            )
        )
        return [handle]

    monkeypatch.setattr(backend, "list", publish_after_record_listing)
    try:
        assert reap(store, backend, now) == []
        assert backend.poll(handles[0]).alive
    finally:
        for handle in handles:
            backend.terminate(handle)


def test_rev001_sweep_releases_capacity_preserves_history_and_recovers_owned_compute(workflow_app):
    app, owner, headers, repo = workflow_app
    with TestClient(app, base_url="https://testserver") as client:
        sessions = []
        for _ in range(3):
            sid = create_session(client, headers, repository={"repo": repo, "ref": "main"})[
                "session"
            ]["id"]
            wait_session(client, headers, sid, "finished")
            rec = idle(app, app.state.task_store.get(sid).agent_id)
            sessions.append((sid, rec))
        account = app.state.hosted_accounts.scoped(owner.id).list()[0]
        assert app.state.hosted_accounts.scoped(owner.id).running_count(account.id) == 3
        catalog = client.get("/v1/providers", headers=headers).json()["providers"]
        assert next(p for p in catalog if p["provider"] == "codex")["readiness"] == "busy"
        for sid, rec in sessions:
            rec.last_activity_at = datetime.now(UTC) - timedelta(hours=1)
            app.state.plane.store.put(rec)
        result = HostedLifecycle(app).sweep()
        assert result["action_kinds"]["suspended"] == 3
        assert app.state.hosted_accounts.running_count(account.id) == 0
        assert app.state.database_records.get("hosted_lifecycle", "latest")
        sid, rec = sessions[0]
        old_id = rec.sandbox_id
        follow = client.post(
            f"/v2/sessions/{sid}/messages", json={"prompt": "Fix greeting"}, headers=headers
        )
        assert follow.status_code == 202, follow.text
        until = time.monotonic() + 15
        while time.monotonic() < until:
            runs = app.state.run_store.list(rec.id)
            if len(runs) == 2 and runs[-1].terminal:
                break
            time.sleep(0.05)
        assert runs[-1].status == "FINISHED"
        assert idle(app, rec.id).sandbox_id != old_id
        assert len(client.get(f"/v2/sessions/{sid}/history", headers=headers).json()["runs"]) == 2


def test_rev001_new_turn_supersedes_stale_idle_termination(monkeypatch):
    from control.backend import LocalProcessBackend
    from control.hosted_lifecycle import SweepPlane
    from control.reaper import reap
    from control.store import InMemoryStore, SessionRecord

    backend, store = LocalProcessBackend(), InMemoryStore()
    now = datetime.now(UTC)
    before = now - timedelta(hours=1)
    handle = backend.create(SandboxSpec(tags={"session_id": "race", "owner": "owner"}))
    store.put(
        SessionRecord(
            id="race",
            title="race",
            status="idle",
            created_at=before,
            updated_at=before,
            model="gpt-6.1-sol",
            turns=1,
            usage=None,
            messages=[],
            owner="owner",
            sandbox_id=handle.id,
            sandbox_root=str(handle.root),
            sandbox_tags=handle.tags,
        )
    )
    original_poll = backend.poll

    def begin_turn_during_poll(handle):
        current = store.get("race")
        current.status, current.updated_at = "running", now
        current.current_turn_id = "turn-2"
        store.put(current)
        return original_poll(handle)

    monkeypatch.setattr(backend, "poll", begin_turn_during_poll)
    plane = SimpleNamespace(
        store=store,
        backend=backend,
        _lock=threading.RLock(),
        clock=lambda: now,
        checkpoints=SimpleNamespace(suspend=lambda *a: False),
    )
    sweep = SweepPlane(plane)
    try:
        reap(sweep.store, sweep.backend, now, checkpoints=sweep.checkpoints)
        assert store.get("race").status == "running"
        assert original_poll(handle).alive
    finally:
        backend.terminate(handle)


def test_rev002_hosted_picker_and_execution_share_provider_truth(workflow_app, monkeypatch):
    app, _, headers, repo = workflow_app
    monkeypatch.delenv("SBX_PROVIDERS", raising=False)
    with TestClient(app, base_url="https://testserver") as client:
        providers = client.get("/v1/providers", headers=headers).json()["providers"]
        codex = next(p for p in providers if p["provider"] == "codex")
        assert codex["runtime"]["enabled"] and codex["readiness"] == "ready"
        assert all(p["readiness"] == "disabled" for p in providers if p != codex)
        models = client.get("/v1/models", headers=headers).json()["models"]
        assert models and all(m["provider"] == "codex" for m in models)
        assert any(m["availability"] == "available" for m in models)
        created = create_session(
            client,
            headers,
            execution={"provider": "codex"},
            repository={"repo": repo, "ref": "main"},
        )
        wait_session(client, headers, created["session"]["id"], "finished")


def test_rev004_ready_upgrade_smokes_before_switch_and_keeps_existing_handle(
    workflow_app, monkeypatch
):
    from control import modal_connection

    app, owner, _, _ = workflow_app
    service = app.state.modal_connections
    backend = app.state.plane.backend
    spec = SandboxSpec(tags={"owner": owner.id, "session_id": "agent", "provider": "codex"})
    old = backend.create(spec)
    old_image = old.tags["runtime_image"]
    monkeypatch.setattr(modal_connection, "runtime_build_identity", lambda _: "b" * 64)
    reconstructed = ModalConnectionService(service.store, service.provider)
    upgraded = reconstructed.provision(owner.id)
    assert upgraded["metadata"]["build_id"] == "b" * 64
    assert upgraded["metadata"]["image"] != old_image
    assert backend.poll(old).alive
    newer = backend.create(spec)
    assert newer.tags["runtime_image"] == upgraded["metadata"]["image"]
    calls = list(service.provider.calls)
    assert reconstructed.provision(owner.id) == upgraded
    assert calls == service.provider.calls
    monkeypatch.setattr(modal_connection, "runtime_build_identity", lambda _: "c" * 64)
    service.provider.fail_step = "smoke"
    with pytest.raises(HostedAuthError):
        reconstructed.provision(owner.id)
    assert service.store.get(owner.id, "modal").metadata["image"] == upgraded["metadata"]["image"]
    assert backend.poll(old).alive
    backend.terminate(old)
    backend.terminate(newer)


@pytest.mark.parametrize("dirty", ["tracked", "untracked", "deleted"])
def test_rev005_rollout_refuses_dirty_source_before_remote_writes(tmp_path, monkeypatch, dirty):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "control").mkdir()
    path = tmp_path / "control" / "committed.py"
    path.write_text("REVIEWED = True\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=t@example.test",
            "commit",
            "-qm",
            "reviewed",
        ],
        cwd=tmp_path,
        check=True,
    )
    monkeypatch.setattr(rollout, "ROOT", tmp_path)
    commit = rollout.reviewed_commit()
    payload, _ = rollout.source_archive(commit)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        assert archive.extractfile("control/committed.py").read() == path.read_bytes()
    if dirty == "tracked":
        path.write_text("DIRTY = True\n")
    elif dirty == "deleted":
        path.unlink()
    else:
        (tmp_path / "control" / "untracked.py").write_text("DIRTY = True\n")
    writes = []
    monkeypatch.setattr(rollout, "ssh", lambda *a, **kw: writes.append(a))
    with pytest.raises(ValueError, match="clean_reviewed_commit"):
        rollout.main()
    assert writes == []
    # An explicitly pinned archive is still exactly committed bytes.
    assert rollout.source_archive(commit) == (payload, rollout.source_archive(commit)[1])


@pytest.mark.parametrize(
    "code,status",
    [
        ("provider_exhausted", "error"),
        ("workspace_invalid", "error"),
        ("modal_provider_unavailable", "error"),
        ("provider_exhausted", "failed"),
        ("cancelled", "cancelled"),
    ],
)
def test_rev009_unbound_review_terminal_state_beats_missing_agent(workflow_app, code, status):
    from control.tasks import TaskRecord

    app, owner, headers, _ = workflow_app
    task = TaskRecord(
        id="sess_failedreview",
        owner=owner.id,
        status="queued" if status == "error" else status,
        request={},
        resolved=None,
        agent_id=None,
        run_id=None,
        created_at="2026-10-04T00:00:00Z",
        updated_at="2026-10-04T00:00:00Z",
        transitions=[{"reason": "dispatch_failed", "detail": {"code": code}}],
    )
    app.state.task_store.put(task)
    if status == "error":
        from control.api_v1.errors import V1ApiError
        from control.api_v2.routes import _mark_dispatch_failed

        _mark_dispatch_failed(app.state.task_store, task.id, V1ApiError(409, code, code))
    app.state.database_records.put_owned(
        "hosted_review_sessions", task.id, owner.id, {"reviewer_session_id": task.id}
    )
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(f"/hosted/review-sessions/{task.id}", headers=headers)
        assert response.json()["status"] == "failed"
        assert response.json()["retryable"]
        assert client.get(f"/v2/sessions/{task.id}", headers=headers).json()["session"][
            "status"
        ] in {
            "failed",
            "cancelled",
        }
