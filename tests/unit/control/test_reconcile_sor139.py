"""SOR-139: watcher-less turn reconciliation.

A provider turn that wrote ``turns/<n>.json`` proved it ended, but a
control-plane restart/cutover mid-turn kills the watcher before it can
settle: the record strands ``running`` (publishes 409, follow-ups 409)
until the reaper's stale rule marks the session ``lost`` and tears the
sandbox down — ledger truth never lands. ``reconcile_turn`` folds the same
sandbox evidence a live watcher would: the run lands its durable terminal
status, the agent returns to idle, and the sandbox stays publish-ready.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from control.backend import LocalProcessBackend, SandboxHandle, SandboxSpec
from control.run_store import InMemoryRunStore, RunLedger
from control.sandbox_io import write_file
from control.service import ControlPlane, LiveTurn
from control.store import InMemoryStore, SessionRecord, empty_usage
from control.workspace import (
    InMemoryWorkspaceStore,
    WorkspaceService,
    WorkspaceSpec,
)

RUNNER = [
    sys.executable,
    str(Path(__file__).resolve().parents[3] / "tests" / "fakes" / "stub_runner.py"),
]

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "sbx-test",
    "GIT_AUTHOR_EMAIL": "sbx-test@localhost",
    "GIT_COMMITTER_NAME": "sbx-test",
    "GIT_COMMITTER_EMAIL": "sbx-test@localhost",
}


def _now() -> datetime:
    return datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def _host_git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def _make_repo(root: Path) -> tuple[Path, str]:
    repo = root / "origin"
    repo.mkdir()
    _host_git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _host_git(repo, "add", "-A")
    _host_git(repo, "commit", "-qm", "A")
    return repo, _host_git(repo, "rev-parse", "HEAD")


def _plane(
    backend: LocalProcessBackend,
    store: InMemoryStore,
    *,
    workspaces: WorkspaceService | None = None,
) -> tuple[ControlPlane, RunLedger]:
    ledger = RunLedger(InMemoryRunStore(), clock=_now)
    plane = ControlPlane(
        backend, store, RUNNER, clock=_now, run_ledger=ledger, workspaces=workspaces
    )
    return plane, ledger


def _running_rec(session_id: str, handle: SandboxHandle | None, n: int = 1) -> SessionRecord:
    now = _now()
    rec = SessionRecord(
        id=session_id,
        title="t",
        status="running",
        created_at=now,
        updated_at=now,
        model="gpt-5.6-luna",
        turns=0,
        usage=empty_usage(),
        messages=[],
        owner="sbx",
        sandbox_id=handle.id if handle else None,
        sandbox_root=str(handle.root) if handle else None,
        sandbox_tags=dict(handle.tags) if handle else {},
        last_activity_at=now,
    )
    rec.current_turn_id = f"turn-{n}"
    rec.current_turn_n = n
    return rec


def _payload(n: int = 1, *, status: str = "success", message: str = "done") -> dict:
    return {
        "n": n,
        "status": status,
        "exit_code": 0,
        "duration_s": 1.0,
        "usage": {"input_tokens": 7, "output_tokens": 3},
        "message": message,
        "error": None,
    }


def _stranded(
    backend: LocalProcessBackend,
    store: InMemoryStore,
    ledger: RunLedger,
    *,
    session_id: str = "s1",
    n: int = 1,
) -> SandboxHandle:
    """A running session whose turn-``n`` watcher never came back."""
    handle = backend.create(SandboxSpec(tags={"session_id": session_id}))
    store.put(_running_rec(session_id, handle, n))
    ledger.begin(agent_id=session_id, n=n)
    return handle


def test_reconcile_lands_durable_finished_and_idles_agent() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    plane, ledger = _plane(backend, store)
    handle = _stranded(backend, store, ledger)
    write_file(backend, handle, "turns/1.json", json.dumps(_payload()))

    assert plane.reconcile_turn("s1", 1) is True

    record = ledger.get("s1", 1)
    assert record is not None
    assert record.status == "FINISHED"
    assert record.terminal is True
    assert record.result_text == "done"
    rec = store.get("s1")
    assert rec is not None
    assert rec.status == "idle"
    assert rec.current_turn_id is None
    assert rec.current_turn_n is None
    assert rec.usage["input_tokens"] == 7
    assert rec.usage["output_tokens"] == 3
    assert rec.turns == 1
    assert rec.messages[-1]["role"] == "assistant"
    assert rec.messages[-1]["text"] == "done"
    assert rec.messages[-1]["turn_id"] == "turn-1"
    # Publish-ready: the sandbox survives reconcile — only the lost rule
    # tears down, and reconcile keeps the session out of its path.
    assert backend.poll(handle).alive is True
    backend.terminate(handle)


def test_reconcile_is_idempotent() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    plane, ledger = _plane(backend, store)
    handle = _stranded(backend, store, ledger)
    write_file(backend, handle, "turns/1.json", json.dumps(_payload()))

    assert plane.reconcile_turn("s1", 1) is True
    assert plane.reconcile_turn("s1", 1) is False
    rec = store.get("s1")
    assert rec is not None and rec.status == "idle"
    # The assistant message landed exactly once.
    assert [m["role"] for m in rec.messages] == ["assistant"]
    backend.terminate(handle)


def test_reconcile_provider_failure_records_error_not_lost() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    plane, ledger = _plane(backend, store)
    handle = _stranded(backend, store, ledger)
    write_file(
        backend,
        handle,
        "turns/1.json",
        json.dumps(_payload(status="failed", message="boom")),
    )

    assert plane.reconcile_turn("s1", 1) is True
    record = ledger.get("s1", 1)
    assert record is not None and record.status == "ERROR"
    rec = store.get("s1")
    assert rec is not None and rec.status == "idle"
    assert backend.poll(handle).alive is True
    backend.terminate(handle)


def test_reconcile_without_evidence_leaves_running() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    plane, ledger = _plane(backend, store)
    handle = _stranded(backend, store, ledger)

    assert plane.reconcile_turn("s1", 1) is False
    rec = store.get("s1")
    assert rec is not None
    assert rec.status == "running"
    assert rec.current_turn_n == 1
    record = ledger.get("s1", 1)
    assert record is not None and not record.terminal
    backend.terminate(handle)


def test_reconcile_declines_turn_owned_by_live_watcher() -> None:
    """An in-process watcher owns its turn: reconcile must not double-fold."""
    backend = LocalProcessBackend()
    store = InMemoryStore()
    plane, ledger = _plane(backend, store)
    handle = _stranded(backend, store, ledger)
    plane._live["s1"] = LiveTurn(turn_id="turn-1", n=1, proc=None)
    write_file(backend, handle, "turns/1.json", json.dumps(_payload()))

    assert plane.reconcile_turn("s1", 1) is False
    rec = store.get("s1")
    assert rec is not None
    assert rec.status == "running"
    assert rec.messages == []
    record = ledger.get("s1", 1)
    assert record is not None and record.status == "RUNNING"
    backend.terminate(handle)


def test_reconcile_declines_terminal_and_idle_sessions() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    plane, ledger = _plane(backend, store)
    handle = _stranded(backend, store, ledger)
    write_file(backend, handle, "turns/1.json", json.dumps(_payload()))
    rec = store.get("s1")
    assert rec is not None
    rec.status = "closed"
    store.put(rec)

    assert plane.reconcile_turn("s1", 1) is False
    record = ledger.get("s1", 1)
    assert record is not None and record.status == "RUNNING"
    backend.terminate(handle)


def test_reconcile_wrong_turn_number_is_noop() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    plane, ledger = _plane(backend, store)
    handle = _stranded(backend, store, ledger, n=2)
    write_file(backend, handle, "turns/2.json", json.dumps(_payload(n=2)))

    assert plane.reconcile_turn("s1", 1) is False
    assert plane.reconcile_turn("s1", 2) is True
    record = ledger.get("s1", 2)
    assert record is not None and record.status == "FINISHED"
    backend.terminate(handle)


def test_reconcile_recorded_cancel_still_wins() -> None:
    """finish() is monotonic: a cancel persisted before reconcile stands."""
    backend = LocalProcessBackend()
    store = InMemoryStore()
    plane, ledger = _plane(backend, store)
    handle = _stranded(backend, store, ledger)
    write_file(backend, handle, "turns/1.json", json.dumps(_payload()))
    ledger.cancel("s1", 1)

    assert plane.reconcile_turn("s1", 1) is True
    record = ledger.get("s1", 1)
    assert record is not None and record.status == "CANCELLED"
    rec = store.get("s1")
    assert rec is not None and rec.status == "idle"
    backend.terminate(handle)


def test_reconcile_turns_sweeps_only_stranded_sessions() -> None:
    backend = LocalProcessBackend()
    store = InMemoryStore()
    plane, ledger = _plane(backend, store)
    stranded = _stranded(backend, store, ledger, session_id="s1")
    write_file(backend, stranded, "turns/1.json", json.dumps(_payload()))
    watched = _stranded(backend, store, ledger, session_id="s2")
    write_file(backend, watched, "turns/1.json", json.dumps(_payload()))
    plane._live["s2"] = LiveTurn(turn_id="turn-1", n=1, proc=None)
    quiet = _stranded(backend, store, ledger, session_id="s3")

    settled = plane.reconcile_turns()

    assert settled == ["s1"]
    assert store.get("s1").status == "idle"  # type: ignore[union-attr]
    assert store.get("s2").status == "running"  # type: ignore[union-attr]
    assert store.get("s3").status == "running"  # type: ignore[union-attr]
    for h in (stranded, watched, quiet):
        backend.terminate(h)


def test_reconcile_refreshes_workspace_head(tmp_path: Path) -> None:
    """Publish-ready means the recorded head matches what the turn left."""
    backend = LocalProcessBackend()
    store = InMemoryStore()
    workspaces = WorkspaceService(backend, InMemoryWorkspaceStore())
    plane, ledger = _plane(backend, store, workspaces=workspaces)
    handle = backend.create(SandboxSpec(tags={"session_id": "s1"}))
    repo, base = _make_repo(tmp_path)
    workspaces.prepare(handle, "s1", WorkspaceSpec(repo=str(repo), base_ref="main", base_sha=base))
    store.put(_running_rec("s1", handle))
    ledger.begin(agent_id="s1", n=1)

    workdir = handle.root / "repo"
    (workdir / "b.txt").write_text("two\n", encoding="utf-8")
    _host_git(workdir, "add", "-A")
    _host_git(workdir, "commit", "-qm", "B")
    head = _host_git(workdir, "rev-parse", "HEAD")
    assert head != base
    write_file(backend, handle, "turns/1.json", json.dumps(_payload()))

    assert plane.reconcile_turn("s1", 1) is True
    record = workspaces.get("s1")
    assert record is not None and record.head_sha == head
    backend.terminate(handle)


def test_reconcile_survives_missing_workspace_and_dead_sandbox(tmp_path: Path) -> None:
    """A workspace refresh failure must never wedge finalization."""
    backend = LocalProcessBackend()
    store = InMemoryStore()
    workspaces = WorkspaceService(backend, InMemoryWorkspaceStore())
    plane, ledger = _plane(backend, store, workspaces=workspaces)
    handle = _stranded(backend, store, ledger)
    write_file(backend, handle, "turns/1.json", json.dumps(_payload()))
    # No workspace declared for s1 — refresh is a no-op.
    assert plane.reconcile_turn("s1", 1) is True
    assert ledger.get("s1", 1).status == "FINISHED"  # type: ignore[union-attr]
    backend.terminate(handle)
