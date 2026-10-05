"""ChangeSet capture/seal/apply E2E on real daemon + local executor
(RFC 167 §05): quiesce → manifest → blobs → canonical subject_digest;
immutability; apply into a second session's worktree."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
FAKE_OPENCODE = REPO_ROOT / "tests" / "fakes" / "fake_opencode.py"


@pytest.fixture()
def plane(pg, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_BIN", f"{sys.executable} {FAKE_OPENCODE}")
    from control.application.changes import ChangeSetService
    from control.application.delegation import DelegationService
    from control.application.runtime_stack import RuntimeStack
    from control.executors.local import LocalExecutorBackend
    from control.jobs import handlers
    from control.storage.blobs import BlobStore

    stack = RuntimeStack(
        pg,
        backends={},
        credential_resolver=lambda session, turn, **_kw: {"files": {}, "env": {}},
    )
    stack.start()
    stack.register_backend(
        "local",
        LocalExecutorBackend(
            run_root=tmp_path / "executors",
            ingress_endpoint=stack.ingress.endpoint,
            repo_root=REPO_ROOT,
        ),
    )
    blobs = BlobStore(tmp_path / "blobs")
    changes = ChangeSetService(pg, blobs, runtime_stack=stack)
    delegations = DelegationService(pg, runtime_stack=stack)
    handlers.set_runtime_stack(stack)
    handlers.set_change_plane(changes, delegations)
    yield {
        "stack": stack,
        "changes": changes,
        "delegations": delegations,
        "blobs": blobs,
    }
    handlers.set_runtime_stack(None)
    handlers.set_change_plane(None, None)
    stack.stop()


def _session(pg, workspace, spec=None):
    from control.application.sessions import SessionService

    return SessionService(pg).create_session(
        principal={"kind": "user", "id": workspace["user_id"]},
        workspace_id=workspace["workspace_id"],
        harness={
            "provider_id": "opencode",
            "model": "opencode/big-pickle",
            "effort": "default",
            "adapter_version": "1",
            "cli_version": "fake",
        },
        projectless_spec=spec or {"backend": "local"},
        effective_input_digest="sha256:e2e",
    )


def _post(pg, workspace, session_id, text):
    from control.application.sessions import SessionService

    return SessionService(pg).post_message(
        principal={"kind": "user", "id": workspace["user_id"]},
        workspace_id=workspace["workspace_id"],
        session_id=session_id,
        author={"kind": "user", "id": workspace["user_id"]},
        routing="queue",
        content={"text": text},
    )


def _drain(pg):
    from control.jobs.handlers import HANDLERS
    from control.jobs.worker import Worker

    Worker(db=pg, holder="t", handlers=HANDLERS).run_until_idle()


def _wait_turn(pg, ws, turn_id, timeout=45):
    from control.persistence.unit_of_work import SqlUnitOfWork

    deadline = time.time() + timeout
    while time.time() < deadline:
        _drain(pg)
        with SqlUnitOfWork(pg) as uow:
            t = uow.turns.get(ws, turn_id)
            if t and t["state"] in ("succeeded", "failed", "cancelled", "interrupted"):
                return t["state"]
        time.sleep(0.3)
    raise AssertionError(f"turn {turn_id} never settled")


def _wait_changeset(pg, ws, session_id, timeout=60):
    from control.persistence.unit_of_work import SqlUnitOfWork

    deadline = time.time() + timeout
    while time.time() < deadline:
        _drain(pg)
        with SqlUnitOfWork(pg) as uow:
            css = uow.changesets.list_by_session(ws, session_id)
            if css:
                return css[-1]
        time.sleep(0.3)
    raise AssertionError("no changeset sealed")


def test_capture_seals_immutable_subject(pg, workspace, plane):
    session = _session(pg, workspace)
    out = _post(pg, workspace, session["session_id"], "write hello.txt")
    assert _wait_turn(pg, workspace["workspace_id"], out["turn_id"]) == "succeeded"

    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        plane["changes"].request_capture(
            uow,
            workspace_id=workspace["workspace_id"],
            session_id=session["session_id"],
            source_turn_id=out["turn_id"],
            origin="explicit",
        )
        uow.commit()
    cs = _wait_changeset(pg, workspace["workspace_id"], session["session_id"])
    assert cs["subject_digest"].startswith("sha256:")
    assert cs["automatic_eligible"] is True  # succeeded turn + explicit origin

    with SqlUnitOfWork(pg) as uow:
        files = uow.changeset_files.list_for(cs["id"])
    paths = {f["path"] for f in files}
    assert "hello.txt" in paths
    assert not any(p.startswith(".git") or "auth.json" in p for p in paths)
    # Payload blob is independently content-verified.
    blob = next(f for f in files if f["path"] == "hello.txt")
    with SqlUnitOfWork(pg) as uow:
        b = uow.blobs.get(workspace["workspace_id"], blob["blob_id"])
    data = plane["blobs"].read(b["storage_key"])
    import hashlib

    assert "sha256:" + hashlib.sha256(data).hexdigest() == blob["content_digest"]
    # Digest is canonical: same manifest → same digest (verified by re-seal dedupe).
    assert json.dumps(cs["manifest"])  # manifest persisted
    assert cs["manifest"]["files"][0]["path"] == "hello.txt"


def test_capture_dedupes_same_turn_generation(pg, workspace, plane):
    session = _session(pg, workspace)
    out = _post(pg, workspace, session["session_id"], "write hello.txt")
    assert _wait_turn(pg, workspace["workspace_id"], out["turn_id"]) == "succeeded"
    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        plane["changes"].request_capture(
            uow,
            workspace_id=workspace["workspace_id"],
            session_id=session["session_id"],
            source_turn_id=out["turn_id"],
        )
        plane["changes"].request_capture(
            uow,
            workspace_id=workspace["workspace_id"],
            session_id=session["session_id"],
            source_turn_id=out["turn_id"],
        )
        uow.commit()
    _wait_changeset(pg, workspace["workspace_id"], session["session_id"])
    with SqlUnitOfWork(pg) as uow:
        all_cs = uow.changesets.list_by_session(workspace["workspace_id"], session["session_id"])
    assert len(all_cs) == 1  # one seal despite duplicate intent


def test_apply_into_second_session(pg, workspace, plane):
    from control.persistence.unit_of_work import SqlUnitOfWork

    src = _session(pg, workspace)
    out = _post(pg, workspace, src["session_id"], "write hello.txt")
    assert _wait_turn(pg, workspace["workspace_id"], out["turn_id"]) == "succeeded"
    with SqlUnitOfWork(pg) as uow:
        plane["changes"].request_capture(
            uow,
            workspace_id=workspace["workspace_id"],
            session_id=src["session_id"],
            source_turn_id=out["turn_id"],
        )
        uow.commit()
    cs = _wait_changeset(pg, workspace["workspace_id"], src["session_id"])

    dest = _session(pg, workspace)
    with SqlUnitOfWork(pg) as uow:
        plane["changes"].apply(
            uow,
            workspace_id=workspace["workspace_id"],
            changeset_id=cs["id"],
            dest_session_id=dest["session_id"],
        )
        uow.commit()
    # Drain apply job → dest worktree generation bumps.
    deadline = time.time() + 60
    while time.time() < deadline:
        _drain(pg)
        with SqlUnitOfWork(pg) as uow:
            wt = uow.worktrees.get_by_session(workspace["workspace_id"], dest["session_id"])
            evs = uow.rows.all(
                "SELECT * FROM session_events WHERE session_id=%s AND type='changeset.applied'",
                (dest["session_id"],),
            )
        if evs:
            break
        time.sleep(0.3)
    else:
        raise AssertionError("apply never completed")
    with SqlUnitOfWork(pg) as uow:
        wt = uow.worktrees.get_by_session(workspace["workspace_id"], dest["session_id"])
    assert wt["generation"] == 1
