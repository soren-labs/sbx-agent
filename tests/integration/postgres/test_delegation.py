"""Delegation E2E (RFC 167 §05): atomic spawn (child Session + Worktree +
delegation + first turn), typed ResultContract validation, wait/wake,
cancel, and budget enforcement — all on a real daemon + local executor."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
FAKE_OPENCODE = REPO_ROOT / "tests" / "fakes" / "fake_opencode.py"

REVIEW_RESULT = {
    "kind": "ReviewAssessment",
    "verdict": "approve",
    "findings": [],
    "subject_digest": "sha256:" + "c" * 64,
    "head_sha": "b" * 40,
}


@pytest.fixture()
def plane(pg, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_BIN", f"{sys.executable} {FAKE_OPENCODE}")
    from control.application.changes import ChangeSetService
    from control.application.delegation import DelegationService
    from control.application.runtime_stack import RuntimeStack
    from control.executors.local import LocalExecutorBackend
    from control.jobs import handlers
    from control.storage.blobs import BlobStore

    result_holder: dict[str, str | None] = {"json": json.dumps(REVIEW_RESULT)}

    def resolver(session, turn, **_kw):
        env = {}
        if result_holder["json"] is not None:
            env["FAKE_RESULT_JSON"] = result_holder["json"]
        return {"files": {}, "env": env}

    stack = RuntimeStack(pg, backends={}, credential_resolver=resolver)
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
        "result_holder": result_holder,
    }
    handlers.set_runtime_stack(None)
    handlers.set_change_plane(None, None)
    stack.stop()


def _session(pg, workspace):
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
        projectless_spec={"backend": "local"},
        effective_input_digest="sha256:e2e",
    )


def _drain(pg):
    from control.jobs.handlers import HANDLERS
    from control.jobs.worker import Worker

    Worker(db=pg, holder="t", handlers=HANDLERS).run_until_idle()


def _wait_delegation(pg, ws, delegation_id, timeout=90):
    from control.persistence.unit_of_work import SqlUnitOfWork

    deadline = time.time() + timeout
    while time.time() < deadline:
        _drain(pg)
        with SqlUnitOfWork(pg) as uow:
            d = uow.delegations.get(ws, delegation_id)
            if d and d["state"] in ("succeeded", "failed", "cancelled", "expired"):
                return d
        time.sleep(0.4)
    raise AssertionError(f"delegation {delegation_id} never settled")


def _spawn(
    pg,
    workspace,
    plane,
    parent_id,
    *,
    contract=None,
    role="reviewer",
    inputs=None,
    prompt="review the changeset",
    budget=None,
):
    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        out = plane["delegations"].spawn(
            uow,
            principal={"kind": "user", "id": workspace["user_id"]},
            workspace_id=workspace["workspace_id"],
            parent_session_id=parent_id,
            role=role,
            result_contract=contract or {"kind": "ReviewAssessment", "schema_version": 1},
            inputs=inputs or [],
            prompt=prompt,
            harness={
                "provider_id": "opencode",
                "model": "opencode/big-pickle",
                "effort": "default",
                "adapter_version": "1",
                "cli_version": "fake",
            },
            projectless_spec={"backend": "local"},
            budget=budget,
        )
        uow.commit()
    return out


def test_spawn_wait_and_result(pg, workspace, plane):
    ws = workspace["workspace_id"]
    parent = _session(pg, workspace)
    out = _spawn(pg, workspace, plane, parent["session_id"])
    assert out["delegation_id"].startswith("del_")
    assert out["child_session_id"].startswith("sess_")

    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        d = uow.delegations.get(ws, out["delegation_id"])
        assert d["state"] == "active"
        child = uow.sessions.get(ws, out["child_session_id"])
        assert child["linked_from_session_id"] == parent["session_id"]
        # First turn was queued atomically inside spawn.
        turn = uow.turns.get(ws, out["turn_id"])
        assert turn["state"] == "queued"

    # Subscribe the parent BEFORE the result lands.
    with SqlUnitOfWork(pg) as uow:
        w = plane["delegations"].wait(
            uow,
            workspace_id=ws,
            subscriber_session_id=parent["session_id"],
            delegation_id=out["delegation_id"],
        )
        uow.commit()
    assert w["state"] == "pending"

    d = _wait_delegation(pg, ws, out["delegation_id"])
    assert d["state"] == "succeeded", d

    with SqlUnitOfWork(pg) as uow:
        result = plane["delegations"].result(
            uow, workspace_id=ws, delegation_id=out["delegation_id"]
        )
        assert result is not None
        assert result["validation_status"] == "valid"
        assert result["verdict"] == "approve"
        assert result["subject_digest"] == REVIEW_RESULT["subject_digest"]
        assert (result["evidence_refs"] or [{}])[0].get("path") == ".sbx/result.json"

        # Waiter satisfied + durably woken (a system turn queued on parent).
        sub = uow.wait_subscriptions.get(ws, w["wait_id"])
        assert sub["state"] == "satisfied"
        assert sub["satisfied_by_result_id"] == result["id"]
        wake_turns = uow.rows.all(
            "SELECT t.* FROM turns t JOIN messages m ON m.id=t.message_id"
            " WHERE t.session_id=%s AND m.role='system' ORDER BY t.ordinal",
            (parent["session_id"],),
        )
        assert wake_turns, "subscriber was not woken"


def test_wait_on_terminal_delegation_satisfies_immediately(pg, workspace, plane):
    ws = workspace["workspace_id"]
    parent = _session(pg, workspace)
    out = _spawn(pg, workspace, plane, parent["session_id"])
    d = _wait_delegation(pg, ws, out["delegation_id"])
    assert d["state"] == "succeeded"

    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        w = plane["delegations"].wait(
            uow,
            workspace_id=ws,
            subscriber_session_id=parent["session_id"],
            delegation_id=out["delegation_id"],
        )
        assert w["state"] == "satisfied"
        # Late-wait still queues a durable wake message.
        sub = uow.wait_subscriptions.get(ws, w["wait_id"])
        assert sub["state"] == "satisfied"
        uow.commit()


def test_invalid_result_fails_delegation(pg, workspace, plane):
    ws = workspace["workspace_id"]
    plane["result_holder"]["json"] = json.dumps({"kind": "ReviewAssessment", "verdict": "maybe"})
    parent = _session(pg, workspace)
    out = _spawn(pg, workspace, plane, parent["session_id"])
    d = _wait_delegation(pg, ws, out["delegation_id"])
    assert d["state"] == "failed"

    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        result = plane["delegations"].result(
            uow, workspace_id=ws, delegation_id=out["delegation_id"]
        )
        assert result["validation_status"] == "invalid"


def test_subject_pin_mismatch_fails_delegation(pg, workspace, plane):
    ws = workspace["workspace_id"]
    parent = _session(pg, workspace)
    contract = {
        "kind": "ReviewAssessment",
        "schema_version": 1,
        "subject_pins": [{"digest": "sha256:" + "0" * 64}],
    }
    out = _spawn(pg, workspace, plane, parent["session_id"], contract=contract)
    d = _wait_delegation(pg, ws, out["delegation_id"])
    assert d["state"] == "failed"

    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        result = plane["delegations"].result(
            uow, workspace_id=ws, delegation_id=out["delegation_id"]
        )
        assert result["validation_status"] == "invalid"


def test_children_budget_cap(pg, workspace, plane):
    ws = workspace["workspace_id"]
    parent = _session(pg, workspace)
    budget = {"max_children": 1, "max_depth": 4, "deadline_seconds": 60}
    _spawn(pg, workspace, plane, parent["session_id"], budget=budget)

    from control.domain.errors import DomainError
    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        with pytest.raises(DomainError):
            plane["delegations"].spawn(
                uow,
                principal={"kind": "user", "id": workspace["user_id"]},
                workspace_id=ws,
                parent_session_id=parent["session_id"],
                role="reviewer",
                result_contract={"kind": "GenericResult", "schema_version": 1},
                inputs=[],
                prompt="second child",
                harness={
                    "provider_id": "opencode",
                    "model": "opencode/big-pickle",
                    "effort": "default",
                    "adapter_version": "1",
                    "cli_version": "fake",
                },
                projectless_spec={"backend": "local"},
                budget=budget,
            )


def test_cancel_marks_delegation_and_waiters(pg, workspace, plane):
    ws = workspace["workspace_id"]
    parent = _session(pg, workspace)
    out = _spawn(pg, workspace, plane, parent["session_id"])

    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        w = plane["delegations"].wait(
            uow,
            workspace_id=ws,
            subscriber_session_id=parent["session_id"],
            delegation_id=out["delegation_id"],
        )
        r = plane["delegations"].cancel(uow, workspace_id=ws, delegation_id=out["delegation_id"])
        assert r["state"] == "cancelled"
        sub = uow.wait_subscriptions.get(ws, w["wait_id"])
        assert sub["state"] == "cancelled"
        uow.commit()

    with SqlUnitOfWork(pg) as uow:
        d = uow.delegations.get(ws, out["delegation_id"])
        assert d["state"] == "cancelled"
        child_turn = uow.turns.get(ws, out["turn_id"])
        assert child_turn["state"] in ("cancelled", "queued", "preparing")


def test_input_digest_pin_enforced(pg, workspace, plane):
    ws = workspace["workspace_id"]
    parent = _session(pg, workspace)

    from control.domain import ids
    from control.domain.errors import DomainError
    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        wt = uow.worktrees.get_by_session(ws, parent["session_id"])["id"]
        cs = ids.new_id("changeset")
        uow.changesets.insert(
            {
                "id": cs,
                "workspace_id": ws,
                "session_id": parent["session_id"],
                "worktree_id": wt,
                "worktree_generation": 1,
                "subject_digest": "sha256:" + "c" * 64,
                "manifest_version": 1,
                "capture_origin": "explicit",
                "manifest": {"entries": []},
            }
        )
        uow.commit()
        with pytest.raises(DomainError):
            plane["delegations"].spawn(
                uow,
                principal={"kind": "user", "id": workspace["user_id"]},
                workspace_id=ws,
                parent_session_id=parent["session_id"],
                role="reviewer",
                result_contract={"kind": "GenericResult", "schema_version": 1},
                inputs=[{"kind": "changeset", "ref": cs, "digest": "sha256:" + "9" * 64}],
                prompt="pinned digest must match",
                harness={
                    "provider_id": "opencode",
                    "model": "opencode/big-pickle",
                    "effort": "default",
                    "adapter_version": "1",
                    "cli_version": "fake",
                },
                projectless_spec={"backend": "local"},
            )
