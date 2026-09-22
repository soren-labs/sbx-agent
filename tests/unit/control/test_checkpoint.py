"""SOR-180: same-Agent checkpoint / suspend / recover (control-plane)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from control.backend import LocalProcessBackend, SandboxHandle, SandboxSpec
from control.checkpoint import (
    CHECKPOINT_CHECKPOINTED,
    CHECKPOINT_RESTORED,
    AgentCheckpoint,
    CheckpointService,
    CheckpointUnavailable,
    FileCheckpointStore,
    InMemoryCheckpointStore,
    checkpoint_from_dict,
    checkpoint_to_dict,
)
from control.environment import LocalSnapshotProvider
from control.reaper import reap
from control.service import ControlPlane, SessionConflict
from control.store import InMemoryStore, SessionRecord, empty_usage
from control.workspace import (
    InMemoryWorkspaceStore,
    WorkspaceRecord,
    WorkspaceService,
)

THREAD_ID = "01a09a36-b4fb-7f90-b96e-42adeefa05e0"  # stub_runner DEFAULT_THREAD_ID

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "sbx-test",
    "GIT_AUTHOR_EMAIL": "sbx-test@localhost",
    "GIT_COMMITTER_NAME": "sbx-test",
    "GIT_COMMITTER_EMAIL": "sbx-test@localhost",
}


def host_git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def _now() -> datetime:
    return datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def env(tmp_path: Path, stub_runner: Path):
    backend = LocalProcessBackend()
    store = InMemoryStore()
    plane = ControlPlane(
        backend,
        store,
        [sys.executable, str(stub_runner)],
        clock=_now,
    )
    snapshots = LocalSnapshotProvider(backend, tmp_path / "snapshots")
    checkpoint_store = InMemoryCheckpointStore()
    checkpoints = CheckpointService(backend, checkpoint_store, snapshots=snapshots, clock=_now)
    plane.checkpoints = checkpoints
    yield plane, backend, store, checkpoints, snapshots
    for handle in list(backend.list()):
        backend.terminate(handle)


def _record(
    *, session_id: str, status: str, handle: SandboxHandle | None, last: datetime
) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        title="t",
        status=status,
        created_at=last,
        updated_at=last,
        model="gpt-5.6-luna",
        turns=0,
        usage=empty_usage(),
        messages=[],
        owner="sbx",
        sandbox_id=handle.id if handle else None,
        sandbox_root=str(handle.root) if handle else None,
        sandbox_tags={"session_id": session_id, "owner": "sbx"} if handle else {},
        last_activity_at=last,
    )


def _wait_status(
    store: InMemoryStore, session_id: str, want: str, timeout: float = 15.0
) -> SessionRecord:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = store.get(session_id)
        if rec is not None and rec.status == want:
            return rec
        time.sleep(0.05)
    rec = store.get(session_id)
    raise AssertionError(
        f"{session_id} did not reach {want!r}; last={getattr(rec, 'status', None)!r}"
    )


def _session_json(root: Path) -> dict:
    return json.loads((root / "session.json").read_text(encoding="utf-8"))


def _suspend(
    plane: ControlPlane,
    backend: LocalProcessBackend,
    store: InMemoryStore,
    checkpoints: CheckpointService,
    session_id: str,
) -> None:
    """Idle-timeout a session into the recoverable ``suspended`` state."""
    reap(
        store,
        backend,
        _now() + timedelta(hours=1),
        idle_timeout_s=1800,
        checkpoints=checkpoints,
    )
    rec = store.get(session_id)
    assert rec is not None and rec.status == "suspended"


def _prepare_workspace(
    checkpoints: CheckpointService,
    backend: LocalProcessBackend,
    session_id: str,
    root: Path,
) -> str:
    """Wire a prepared workspace (real git repo at ``root/repo``) for a session."""
    ws_store = InMemoryWorkspaceStore()
    workspaces = WorkspaceService(backend, ws_store)
    workdir = root / "repo"
    workdir.mkdir()
    host_git(workdir, "init", "-q", "-b", "main")
    (workdir / "a.txt").write_text("one\n", encoding="utf-8")
    host_git(workdir, "add", "-A")
    host_git(workdir, "commit", "-qm", "A")
    head = host_git(workdir, "rev-parse", "HEAD")
    ws_store.put(
        WorkspaceRecord(
            agent_id=session_id,
            repo="o/r",
            base_ref="main",
            base_sha=head,
            workdir="repo",
            checkout_sha=head,
            head_sha=head,
        )
    )
    checkpoints._workspaces = workspaces
    return head


# ------------------------------------------------------------ record codec


def test_checkpoint_record_roundtrip() -> None:
    record = AgentCheckpoint(
        agent_id="a1",
        snapshot_ref="im-abc123",
        provider="grok",
        account_id="acct-1",
        native_session_id="thread-9",
        turns=3,
        secrets=["sbx-acct-acct-1"],
        resource_secrets=["sbx-res-x"],
        checkpointed_at="2026-09-13T12:00:00+00:00",
        created_at="2026-09-13T12:00:00+00:00",
        updated_at="2026-09-13T12:00:00+00:00",
    )
    out = checkpoint_from_dict(json.loads(json.dumps(checkpoint_to_dict(record))))
    assert out == record


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"agent_id": "a", "status": "bogus"},
        {"agent_id": "a", "snapshot_ref": "bad ref!"},
        {"agent_id": "a", "turns": -1},
        {"agent_id": "a", "secrets": "sbx-acct-x"},
        "not-a-dict",
    ],
)
def test_checkpoint_record_rejects_bad_payloads(data: object) -> None:
    with pytest.raises(ValueError):
        checkpoint_from_dict(data)


def test_file_checkpoint_store_roundtrip(tmp_path: Path) -> None:
    store = FileCheckpointStore(tmp_path / "checkpoints")
    assert store.get("a1") is None
    store.put(AgentCheckpoint(agent_id="a1", snapshot_ref="ref-1"))
    store.put(AgentCheckpoint(agent_id="b2", status="failed", last_error="boom"))
    assert store.get("a1").snapshot_ref == "ref-1"
    assert {r.agent_id for r in store.list()} == {"a1", "b2"}
    store.delete("a1")
    assert store.get("a1") is None


# ------------------------------------------------- suspend → release → restore


def test_idle_suspend_then_followup_restores_same_agent(env) -> None:
    plane, backend, store, checkpoints, snapshots = env
    session_id = plane.create_session(owner="sbx", title="t", model=None)
    rec = store.get(session_id)
    assert rec is not None and rec.status == "idle"
    old_sandbox_id = rec.sandbox_id

    # Turn 1 establishes the native provider session (thread id) on the fs.
    turn_id = plane.post_message(session_id, "first")
    assert turn_id == "turn-1"
    _wait_status(store, session_id, "idle")
    root = Path(rec.sandbox_root)
    assert _session_json(root)["native_session_id"] == THREAD_ID

    # Native provider session state + workspace file + credential files.
    (root / "marker.txt").write_text("keep-me", encoding="utf-8")
    (root / ".codex" / "sessions" / "abc").mkdir(parents=True)
    (root / ".codex" / "sessions" / "abc" / "rollout.jsonl").write_text("{}\n")
    (root / "home" / ".grok").mkdir(parents=True, exist_ok=True)
    (root / "home" / ".grok" / "auth.json").write_text('{"token":"fixture"}')
    (root / ".codex" / "auth.json").write_text('{"tokens":{"access_token":"fixture"}}')

    actions = reap(
        store,
        backend,
        _now() + timedelta(hours=1),
        idle_timeout_s=1800,
        checkpoints=checkpoints,
    )
    rec = store.get(session_id)
    assert rec is not None and rec.status == "suspended"
    assert rec.sandbox_id == old_sandbox_id  # released sandbox kept as provenance
    assert not backend.poll(SandboxHandle(id=old_sandbox_id, root=root)).alive
    assert any(a.kind == "suspended" and a.session_id == session_id for a in actions)
    assert not any(a.kind == "timed_out" for a in actions)

    # The durable checkpoint carries the native session ref — credentials do not.
    cp = checkpoints.get(session_id)
    assert cp is not None and cp.status == CHECKPOINT_CHECKPOINTED
    assert cp.snapshot_ref and cp.native_session_id == THREAD_ID
    assert cp.turns == 1
    snap_root = snapshots._root / cp.snapshot_ref
    assert (snap_root / "marker.txt").is_file()
    assert (snap_root / ".codex" / "sessions" / "abc" / "rollout.jsonl").is_file()
    assert _session_json(snap_root)["native_session_id"] == THREAD_ID
    assert not (snap_root / ".codex" / "auth.json").exists()
    assert not (snap_root / "home" / ".grok" / "auth.json").exists()

    # The public wire view never exposes the internal suspended state.
    assert plane.public(rec)["status"] == "idle"

    # Follow-up → same Agent id restores the checkpoint and dispatches.
    turn_id = plane.post_message(session_id, "second")
    assert turn_id == "turn-2"
    rec = _wait_status(store, session_id, "idle")
    assert rec.sandbox_id != old_sandbox_id
    new_root = Path(rec.sandbox_root)
    assert (new_root / "marker.txt").is_file()
    assert (new_root / ".codex" / "sessions" / "abc" / "rollout.jsonl").is_file()
    # Same native provider session resumed across the sandbox swap.
    assert _session_json(new_root)["native_session_id"] == THREAD_ID
    assert rec.turns == 2
    cp = checkpoints.get(session_id)
    assert cp is not None and cp.status == CHECKPOINT_RESTORED


def test_restore_reattaches_account_credentials(env, monkeypatch: pytest.MonkeyPatch) -> None:
    blob = {"provider": "grok", "files": {".grok/auth.json": "fixture-token"}}
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", json.dumps(blob))
    monkeypatch.setenv("SBX_ACCOUNT_ID", "acct-1")
    plane, backend, store, checkpoints, _snapshots = env
    session_id = plane.create_session(
        owner="sbx", title="t", model=None, provider="grok", account_id="acct-1"
    )
    rec = store.get(session_id)
    root = Path(rec.sandbox_root)
    # Provision restored the blob; the checkpoint scrub must remove it.
    assert (root / "home" / ".grok" / "auth.json").is_file()

    reap(
        store,
        backend,
        _now() + timedelta(hours=1),
        idle_timeout_s=1800,
        checkpoints=checkpoints,
    )
    assert store.get(session_id).status == "suspended"

    turn_id = plane.post_message(session_id, "again")
    assert turn_id == "turn-1"
    rec = _wait_status(store, session_id, "idle")
    new_root = Path(rec.sandbox_root)
    # Re-attached in-sandbox from the env bridge — never from the snapshot.
    assert (new_root / "home" / ".grok" / "auth.json").read_text() == "fixture-token"
    assert (new_root / "home" / ".grok" / "auth.json").stat().st_mode & 0o777 == 0o600


def test_suspend_persists_spec_secrets_for_reattach(env) -> None:
    plane, backend, store, checkpoints, _snapshots = env
    session_id = plane.open_session(owner="sbx", title="t", model=None)
    plane.provision_session(
        session_id,
        provider="grok",
        account_id="acct-1",
        secret_name="sbx-acct-acct-1",
        resource_secrets=["sbx-res-data"],
    )
    rec = store.get(session_id)
    assert rec.spec_secrets == {
        "secrets": ["sbx-acct-acct-1"],
        "resource_secrets": ["sbx-res-data"],
    }
    reap(
        store,
        backend,
        _now() + timedelta(hours=1),
        idle_timeout_s=1800,
        checkpoints=checkpoints,
    )
    cp = checkpoints.get(session_id)
    assert cp is not None and cp.status == CHECKPOINT_CHECKPOINTED
    assert cp.secrets == ["sbx-acct-acct-1"]
    assert cp.resource_secrets == ["sbx-res-data"]

    # The restored sandbox is created with the same Secret refs.
    specs: list[SandboxSpec] = []
    real_create = backend.create

    def _record_create(spec: SandboxSpec) -> SandboxHandle:
        specs.append(spec)
        return real_create(spec)

    monkey_create = _record_create
    backend.create = monkey_create  # type: ignore[method-assign]
    try:
        plane.recover_session(session_id)
    finally:
        backend.create = real_create  # type: ignore[method-assign]
    assert specs and specs[-1].secrets == ["sbx-acct-acct-1"]
    assert specs[-1].resource_secrets == ["sbx-res-data"]
    assert specs[-1].tags.get("session_id") == session_id


# ------------------------------------------------------------- failure paths


def test_unrecoverable_suspend_falls_back_to_timed_out(env) -> None:
    plane, backend, store, checkpoints, _snapshots = env
    session_id = plane.create_session(owner="sbx", title="t", model=None)
    checkpoints._snapshots = None  # snapshot seam unavailable → no checkpoint
    actions = reap(
        store,
        backend,
        _now() + timedelta(hours=1),
        idle_timeout_s=1800,
        checkpoints=checkpoints,
    )
    rec = store.get(session_id)
    assert rec is not None and rec.status == "timed_out"
    assert any(a.kind == "timed_out" and a.session_id == session_id for a in actions)
    cp = checkpoints.get(session_id)
    assert cp is not None and cp.status == "failed" and cp.last_error


def test_platform_loss_without_checkpoint_is_diagnosed(env) -> None:
    plane, backend, store, checkpoints, _snapshots = env
    handle = backend.create(SandboxSpec(tags={"session_id": "s1", "owner": "sbx"}))
    store.put(_record(session_id="s1", status="idle", handle=handle, last=_now()))
    backend.terminate(handle)  # platform reclaimed it before any checkpoint
    actions = reap(
        store,
        backend,
        _now() + timedelta(seconds=5),
        idle_timeout_s=1800,
        checkpoints=checkpoints,
    )
    rec = store.get("s1")
    assert rec is not None and rec.status == "lost"
    assert any(a.kind == "platform_loss" and a.session_id == "s1" for a in actions), actions
    cp = checkpoints.get("s1")
    assert cp is not None and cp.status == "failed"
    assert "platform loss" in (cp.last_error or "")


def test_platform_loss_with_checkpoint_heals_to_suspended(env) -> None:
    plane, backend, store, checkpoints, _snapshots = env
    handle = backend.create(SandboxSpec(tags={"session_id": "s1", "owner": "sbx"}))
    store.put(_record(session_id="s1", status="idle", handle=handle, last=_now()))
    # Half-finished sweep: checkpoint landed, transition to suspended did not.
    checkpoints.store.put(
        AgentCheckpoint(agent_id="s1", status=CHECKPOINT_CHECKPOINTED, snapshot_ref="ref-1")
    )
    backend.terminate(handle)
    actions = reap(
        store,
        backend,
        _now() + timedelta(seconds=5),
        idle_timeout_s=1800,
        checkpoints=checkpoints,
    )
    rec = store.get("s1")
    assert rec is not None and rec.status == "suspended"
    assert any(a.kind == "suspended" and a.session_id == "s1" for a in actions)


def test_suspended_record_is_skipped_by_reaper(env) -> None:
    plane, backend, store, checkpoints, _snapshots = env
    store.put(
        _record(
            session_id="s1",
            status="suspended",
            handle=None,
            last=_now() - timedelta(hours=9),
        )
    )
    actions = reap(
        store,
        backend,
        _now(),
        idle_timeout_s=60,
        checkpoints=checkpoints,
    )
    assert store.get("s1").status == "suspended"
    assert not actions


def test_restore_without_checkpoint_marks_lost(env) -> None:
    plane, backend, store, checkpoints, _snapshots = env
    store.put(
        _record(
            session_id="s1",
            status="suspended",
            handle=None,
            last=_now() - timedelta(hours=1),
        )
    )
    with pytest.raises(SessionConflict):
        plane.post_message("s1", "hello")
    rec = store.get("s1")
    assert rec is not None and rec.status == "lost"


def test_transient_restore_failure_keeps_suspended_and_retries(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SOR-180: restore is idempotent/retryable — one provider blip must
    not brick a recoverable agent holding a valid checkpoint."""
    plane, backend, store, checkpoints, snapshots = env
    session_id = plane.create_session(owner="sbx", title="t", model=None)
    plane.post_message(session_id, "first")
    _wait_status(store, session_id, "idle")
    _suspend(plane, backend, store, checkpoints, session_id)
    cp = checkpoints.get(session_id)
    assert cp is not None and cp.status == CHECKPOINT_CHECKPOINTED

    real_restore = snapshots.restore
    calls = {"n": 0}

    def flaky_restore(ref: str, spec: SandboxSpec) -> SandboxHandle:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("snapshot provider transient error")
        return real_restore(ref, spec)

    monkeypatch.setattr(snapshots, "restore", flaky_restore)

    with pytest.raises(SessionConflict):
        plane.post_message(session_id, "again")
    rec = store.get(session_id)
    assert rec is not None and rec.status == "suspended"
    cp = checkpoints.get(session_id)
    # The checkpoint stays usable; the failure is only noted.
    assert cp is not None and cp.status == CHECKPOINT_CHECKPOINTED
    assert "transient error" in (cp.last_error or "")

    # The retry re-attempts the restore and dispatches normally.
    turn_id = plane.post_message(session_id, "again")
    assert turn_id == "turn-2"
    rec = _wait_status(store, session_id, "idle")
    assert _session_json(Path(rec.sandbox_root))["native_session_id"] == THREAD_ID
    assert checkpoints.get(session_id).status == CHECKPOINT_RESTORED


def test_transient_reattach_failure_keeps_suspended_and_retries(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed in-sandbox credential reattach is equally retryable —
    the discarded sandbox does not downgrade the checkpoint."""
    plane, backend, store, checkpoints, _snapshots = env
    session_id = plane.create_session(owner="sbx", title="t", model=None)
    plane.post_message(session_id, "first")
    _wait_status(store, session_id, "idle")
    _suspend(plane, backend, store, checkpoints, session_id)

    real_exec = backend.exec
    failures = {"n": 0}

    def flaky_exec(handle, cmd, env=None):
        if cmd[:2] == ["python3", "-c"] and "SBX_ACCOUNT_CREDENTIAL" in cmd[2]:
            failures["n"] += 1
            raise RuntimeError("exec transport error")
        return real_exec(handle, cmd, env=env)

    monkeypatch.setattr(backend, "exec", flaky_exec)
    with pytest.raises(SessionConflict):
        plane.post_message(session_id, "again")
    assert failures["n"] == 1
    rec = store.get(session_id)
    assert rec is not None and rec.status == "suspended"
    cp = checkpoints.get(session_id)
    assert cp is not None and cp.status == CHECKPOINT_CHECKPOINTED

    monkeypatch.setattr(backend, "exec", real_exec)
    turn_id = plane.post_message(session_id, "again")
    assert turn_id == "turn-2"
    _wait_status(store, session_id, "idle")
    assert checkpoints.get(session_id).status == CHECKPOINT_RESTORED


def test_restore_without_snapshot_ref_marks_lost(env) -> None:
    """Terminal ``lost`` is reserved for no usable checkpoint."""
    plane, backend, store, checkpoints, _snapshots = env
    store.put(
        _record(
            session_id="s1",
            status="suspended",
            handle=None,
            last=_now() - timedelta(hours=1),
        )
    )
    checkpoints.store.put(
        AgentCheckpoint(agent_id="s1", status=CHECKPOINT_CHECKPOINTED, snapshot_ref=None)
    )
    with pytest.raises(SessionConflict):
        plane.post_message("s1", "hello")
    rec = store.get("s1")
    assert rec is not None and rec.status == "lost"


def test_foreign_snapshot_fails_closed(env) -> None:
    """SOR-180 #9: post-restore identity validation — a checkpoint record
    whose restored ``session.json`` does not match the recorded native
    session fails closed (session ``lost``, checkpoint ``failed``) instead
    of waking the agent in a foreign filesystem."""
    plane, backend, store, checkpoints, _snapshots = env
    agent_a = plane.create_session(owner="sbx", title="a", model=None)
    plane.post_message(agent_a, "first")
    _wait_status(store, agent_a, "idle")
    agent_b = plane.create_session(owner="sbx", title="b", model=None)
    plane.post_message(agent_b, "first")
    _wait_status(store, agent_b, "idle")

    # Distinguish B's native provider session inside its snapshot.
    b_root = Path(store.get(agent_b).sandbox_root)
    session = _session_json(b_root)
    session["native_session_id"] = "thread-b"
    (b_root / "session.json").write_text(json.dumps(session), encoding="utf-8")

    _suspend(plane, backend, store, checkpoints, agent_a)
    _suspend(plane, backend, store, checkpoints, agent_b)

    # Corruption: A's checkpoint record points at B's snapshot.
    a_rec = checkpoints.get(agent_a)
    b_rec = checkpoints.get(agent_b)
    assert a_rec is not None and b_rec is not None
    a_rec.snapshot_ref = b_rec.snapshot_ref
    checkpoints.store.put(a_rec)

    with pytest.raises(SessionConflict):
        plane.post_message(agent_a, "follow-up")
    rec = store.get(agent_a)
    assert rec is not None and rec.status == "lost"
    cp = checkpoints.get(agent_a)
    assert cp is not None and cp.status == "failed"
    assert "native_session_id mismatch" in (cp.last_error or "")
    # B is untouched and still recoverable.
    assert store.get(agent_b).status == "suspended"


def test_workspace_head_recorded_and_verified(env) -> None:
    """A prepared workspace pins ``workdir`` + HEAD on the checkpoint and
    the restore verifies the restored checkout matches."""
    plane, backend, store, checkpoints, snapshots = env
    session_id = plane.create_session(owner="sbx", title="t", model=None)
    plane.post_message(session_id, "first")
    _wait_status(store, session_id, "idle")
    rec = store.get(session_id)
    head = _prepare_workspace(checkpoints, backend, session_id, Path(rec.sandbox_root))
    _suspend(plane, backend, store, checkpoints, session_id)

    cp = checkpoints.get(session_id)
    assert cp is not None and cp.status == CHECKPOINT_CHECKPOINTED
    assert cp.workspace_workdir == "repo"
    assert cp.workspace_head_sha == head
    snap_root = snapshots._root / cp.snapshot_ref
    assert host_git(snap_root / "repo", "rev-parse", "HEAD") == head

    turn_id = plane.post_message(session_id, "again")
    assert turn_id == "turn-2"
    rec = _wait_status(store, session_id, "idle")
    restored = Path(rec.sandbox_root)
    assert host_git(restored / "repo", "rev-parse", "HEAD") == head
    assert checkpoints.get(session_id).status == CHECKPOINT_RESTORED


def test_workspace_head_mismatch_fails_closed(env) -> None:
    """A restored workspace whose HEAD drifted from the recorded checkpoint
    identity fails closed — the agent never wakes on foreign code."""
    plane, backend, store, checkpoints, snapshots = env
    session_id = plane.create_session(owner="sbx", title="t", model=None)
    plane.post_message(session_id, "first")
    _wait_status(store, session_id, "idle")
    rec = store.get(session_id)
    _prepare_workspace(checkpoints, backend, session_id, Path(rec.sandbox_root))
    _suspend(plane, backend, store, checkpoints, session_id)
    cp = checkpoints.get(session_id)
    assert cp is not None and cp.workspace_head_sha

    # The snapshot content drifts after the checkpoint landed.
    snap_root = snapshots._root / cp.snapshot_ref
    (snap_root / "repo" / "b.txt").write_text("two\n", encoding="utf-8")
    host_git(snap_root / "repo", "add", "-A")
    host_git(snap_root / "repo", "commit", "-qm", "foreign commit")

    with pytest.raises(SessionConflict):
        plane.post_message(session_id, "again")
    rec = store.get(session_id)
    assert rec is not None and rec.status == "lost"
    cp = checkpoints.get(session_id)
    assert cp is not None and cp.status == "failed"
    assert "workspace HEAD mismatch" in (cp.last_error or "")


def test_close_discards_checkpoint(env) -> None:
    plane, backend, store, checkpoints, _snapshots = env
    store.put(
        _record(
            session_id="s1",
            status="suspended",
            handle=None,
            last=_now() - timedelta(hours=1),
        )
    )
    checkpoints.store.put(
        AgentCheckpoint(agent_id="s1", status=CHECKPOINT_CHECKPOINTED, snapshot_ref="ref-1")
    )
    plane.close("s1")
    assert store.get("s1").status == "closed"
    assert checkpoints.get("s1") is None


def test_stop_on_suspended_reports_idle(env) -> None:
    plane, backend, store, checkpoints, _snapshots = env
    store.put(
        _record(
            session_id="s1",
            status="suspended",
            handle=None,
            last=_now() - timedelta(hours=1),
        )
    )
    assert plane.stop("s1") == "idle"


def test_reaper_without_checkpoints_keeps_timed_out(env) -> None:
    """Regression: no checkpoint service → the pre-SOR-180 sweep is unchanged."""
    _plane, backend, store, _checkpoints, _snapshots = env
    handle = backend.create(SandboxSpec(tags={"session_id": "s1", "owner": "sbx"}))
    store.put(_record(session_id="s1", status="idle", handle=handle, last=_now()))
    actions = reap(
        store,
        backend,
        _now() + timedelta(hours=1),
        idle_timeout_s=1800,
        checkpoints=None,
    )
    rec = store.get("s1")
    assert rec is not None and rec.status == "timed_out"
    assert any(a.kind == "timed_out" for a in actions)


def test_checkpoint_unavailable_raised_for_missing_record(env) -> None:
    _plane, _backend, _store, checkpoints, _snapshots = env
    rec = _record(session_id="gone", status="suspended", handle=None, last=_now())
    with pytest.raises(CheckpointUnavailable):
        checkpoints.restore(rec)
