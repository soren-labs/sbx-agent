"""Delivery + MergeRequest E2E on the durable queue (RFC 167 §05):
intent-before-effect, target-claim serialization, exact-subject gates,
CAS push semantics, idempotent step re-entry, derived merge gate."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration

REPO = "soren-labs/sbx-e2e-test"
HEAD_SHA = "b" * 40


def _fabricate_changeset(pg, workspace, session_id, *, head_sha=HEAD_SHA, automatic_eligible=True):
    """Insert a sealed ChangeSet + its worktree — deterministic subject."""
    from control.domain import ids
    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        wt = uow.worktrees.get_by_session(workspace["workspace_id"], session_id)
        wt_id = wt["id"]
        cs_id = ids.new_id("changeset")
        uow.changesets.insert(
            {
                "id": cs_id,
                "workspace_id": workspace["workspace_id"],
                "session_id": session_id,
                "worktree_id": wt_id,
                "worktree_generation": 1,
                "subject_digest": "sha256:" + "c" * 64,
                "manifest_version": 1,
                "capture_origin": "explicit",
                "repository": REPO,
                "head_sha": head_sha,
                "automatic_eligible": automatic_eligible,
                "manifest": {"entries": []},
            }
        )
        uow.commit()
    return cs_id


def _make_delivery_service(pg, remote, tmp_path):
    from control.application.delivery import DeliveryService
    from control.storage.blobs import BlobStore

    return DeliveryService(
        pg,
        remote_factory=lambda delivery, connections: remote,
        blob_store=BlobStore(tmp_path / "blobs"),
        scratch_root=tmp_path / "scratch",
    )


def _drain(pg):
    from control.jobs.handlers import HANDLERS
    from control.jobs.worker import Worker

    Worker(db=pg, holder="t", handlers=HANDLERS).run_until_idle()


def _wait_delivery(pg, ws, delivery_id, timeout=15):
    from control.persistence.unit_of_work import SqlUnitOfWork

    deadline = time.time() + timeout
    while time.time() < deadline:
        _drain(pg)
        with SqlUnitOfWork(pg) as uow:
            d = uow.deliveries.get(ws, delivery_id)
            if d and d["state"] in ("succeeded", "blocked", "failed", "cancelled"):
                return d
        time.sleep(0.2)
    raise AssertionError(f"delivery {delivery_id} never settled")


def _wait_merge(pg, ws, mr_id, timeout=15):
    from control.persistence.unit_of_work import SqlUnitOfWork

    deadline = time.time() + timeout
    while time.time() < deadline:
        _drain(pg)
        with SqlUnitOfWork(pg) as uow:
            mr = uow.merge_requests.get(ws, mr_id)
            if mr and mr["state"] in ("succeeded", "blocked", "failed", "cancelled"):
                return mr
        time.sleep(0.2)
    raise AssertionError(f"merge request {mr_id} never settled")


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


def test_pull_request_delivery_pushes_and_creates_pr(pg, workspace, tmp_path):
    from control.vcs.remote import FakeRemote

    remote = FakeRemote()
    svc = _make_delivery_service(pg, remote, tmp_path)
    from control.jobs import handlers

    handlers.set_delivery_plane(svc)
    session = _session(pg, workspace)
    cs_id = _fabricate_changeset(pg, workspace, session["session_id"])

    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        out = svc.create(
            uow,
            workspace_id=workspace["workspace_id"],
            session_id=session["session_id"],
            changeset_id=cs_id,
            target={"repository": REPO, "base_ref": "main"},
            transport="pull_request",
            ship_policy={"required_result_count": 0},
            authorizing_principal=workspace["user_id"],
        )
        uow.commit()
    d = _wait_delivery(pg, workspace["workspace_id"], out["delivery_id"])
    assert d["state"] == "succeeded", d
    assert d["effect_evidence"]["head_sha"] == HEAD_SHA
    kinds = [entry[0] for entry in remote.log]
    assert "push" in kinds and "pr" in kinds

    # Steps recorded durably with verified evidence.
    with SqlUnitOfWork(pg) as uow:
        steps = uow.delivery_steps.list_for(workspace["workspace_id"], out["delivery_id"])
        assert {s["kind"] for s in steps} == {"verify", "push", "pull_request", "reconcile"}
        assert all(s["state"] == "succeeded" for s in steps)
        # Stable ref pins the exact session/changeset.
        assert out["ref"] == f"sbx/{session['session_id']}/{cs_id}"

    # Idempotent: re-running the perform job touches no remote state.
    log_before = list(remote.log)
    svc.perform_delivery(
        {
            "workspace_id": workspace["workspace_id"],
            "id": "job_re",
            "target_id": out["delivery_id"],
            "payload": {},
        },
        None,
    )
    assert remote.log == log_before
    handlers.set_delivery_plane(None)


def test_delivery_target_claim_serializes_mutations(pg, workspace, tmp_path):
    from control.domain.errors import DomainError
    from control.vcs.remote import FakeRemote

    remote = FakeRemote()
    svc = _make_delivery_service(pg, remote, tmp_path)
    session = _session(pg, workspace)
    cs_id = _fabricate_changeset(pg, workspace, session["session_id"])

    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        out = svc.create(
            uow,
            workspace_id=workspace["workspace_id"],
            session_id=session["session_id"],
            changeset_id=cs_id,
            target={"repository": REPO},
            transport="git_branch",
            ship_policy={"required_result_count": 0},
            authorizing_principal=workspace["user_id"],
        )
        uow.commit()
        with pytest.raises(DomainError):
            svc.create(
                uow,
                workspace_id=workspace["workspace_id"],
                session_id=session["session_id"],
                changeset_id=cs_id,
                target={"repository": REPO, "ref": out["ref"]},
                transport="git_branch",
                ship_policy={"required_result_count": 0},
                authorizing_principal=workspace["user_id"],
            )


def test_push_cas_blocks_on_drift(pg, workspace, tmp_path):
    from control.vcs.remote import FakeRemote

    remote = FakeRemote()
    remote.refs[f"{REPO}@sbx-taken"] = {"sha": "d" * 40}
    svc = _make_delivery_service(pg, remote, tmp_path)
    from control.jobs import handlers

    handlers.set_delivery_plane(svc)
    session = _session(pg, workspace)
    cs_id = _fabricate_changeset(pg, workspace, session["session_id"])

    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        out = svc.create(
            uow,
            workspace_id=workspace["workspace_id"],
            session_id=session["session_id"],
            changeset_id=cs_id,
            target={
                "repository": REPO,
                "ref": "sbx-taken",
                "expected_old_ref_sha": "e" * 40,
            },
            transport="git_branch",
            ship_policy={"required_result_count": 0},
            authorizing_principal=workspace["user_id"],
        )
        uow.commit()
    d = _wait_delivery(pg, workspace["workspace_id"], out["delivery_id"])
    assert d["state"] == "blocked"
    assert "remote_conflict" in (d["effect_evidence"] or {}).get("reason", "")
    handlers.set_delivery_plane(None)


def test_exact_subject_pr_gate_blocks_on_head_drift(pg, workspace, tmp_path):
    from control.vcs.remote import FakeRemote

    remote = FakeRemote()
    svc = _make_delivery_service(pg, remote, tmp_path)
    from control.jobs import handlers

    handlers.set_delivery_plane(svc)
    session = _session(pg, workspace)
    cs_id = _fabricate_changeset(pg, workspace, session["session_id"])
    ref = f"sbx/{session['session_id']}/{cs_id}"
    # Pre-seed an open PR on the same head ref but pointing at a WRONG head.
    remote.prs[(REPO, 7)] = {
        "number": 7,
        "url": "https://fake.local/pr/7",
        "head_ref": ref,
        "base_ref": "main",
        "head_sha": "f" * 40,
        "state": "open",
        "draft": False,
        "merged": False,
    }

    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        out = svc.create(
            uow,
            workspace_id=workspace["workspace_id"],
            session_id=session["session_id"],
            changeset_id=cs_id,
            target={"repository": REPO, "ref": ref, "base_ref": "main"},
            transport="pull_request",
            ship_policy={"required_result_count": 0},
            authorizing_principal=workspace["user_id"],
        )
        uow.commit()
    d = _wait_delivery(pg, workspace["workspace_id"], out["delivery_id"])
    assert d["state"] == "blocked", d["effect_evidence"]
    handlers.set_delivery_plane(None)


def _approve_result(pg, workspace, session_id, subject_digest):
    """Fabricate a valid approving ReviewAssessment from an independent child."""
    from control.domain import ids
    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        child = ids.new_id("session")
        uow.sessions.insert(
            {
                "id": child,
                "workspace_id": workspace["workspace_id"],
                "role": "reviewer",
                "created_by": workspace["user_id"],
                "harness": {"provider_id": "opencode", "model": "m"},
                "effective_input_digest": "sha256:" + "0" * 64,
            }
        )
        d_id = ids.new_id("delegation")
        uow.delegations.insert(
            {
                "id": d_id,
                "workspace_id": workspace["workspace_id"],
                "parent_session_id": session_id,
                "child_session_id": child,
                "role": "reviewer",
                "state": "succeeded",
                "result_contract": {"kind": "ReviewAssessment", "schema_version": 1},
            }
        )
        uow.delegation_results.insert(
            {
                "id": ids.new_id("delegation_result"),
                "workspace_id": workspace["workspace_id"],
                "delegation_id": d_id,
                "child_session_id": child,
                "contract_version": 1,
                "subject_digest": subject_digest,
                "verdict": "approve",
                "validation_status": "valid",
                "value": {"verdict": "approve", "findings": []},
            }
        )
        uow.commit()


def test_merge_gate_and_merge_request(pg, workspace, tmp_path):
    from control.domain.errors import DomainError
    from control.vcs.remote import FakeRemote

    remote = FakeRemote()
    svc = _make_delivery_service(pg, remote, tmp_path)
    from control.jobs import handlers

    handlers.set_delivery_plane(svc)
    session = _session(pg, workspace)
    cs_id = _fabricate_changeset(pg, workspace, session["session_id"])

    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        out = svc.create(
            uow,
            workspace_id=workspace["workspace_id"],
            session_id=session["session_id"],
            changeset_id=cs_id,
            target={"repository": REPO, "base_ref": "main"},
            transport="pull_request",
            ship_policy={"required_result_count": 1},
            authorizing_principal=workspace["user_id"],
        )
        uow.commit()
    d = _wait_delivery(pg, workspace["workspace_id"], out["delivery_id"])
    assert d["state"] == "succeeded"

    ws = workspace["workspace_id"]
    with SqlUnitOfWork(pg) as uow:
        # Gate derived, not verdict: blocked without the independent approval.
        gate = svc.merge_gate(uow, workspace_id=ws, delivery_id=out["delivery_id"])
        assert not gate["eligible"] and gate["approvals"] == 0
        with pytest.raises(DomainError):
            svc.request_merge(
                uow,
                workspace_id=ws,
                delivery_id=out["delivery_id"],
                expected_head_sha=HEAD_SHA,
                merge_method="squash",
                authorizing_principal=workspace["user_id"],
            )
        uow.rollback()

    _approve_result(pg, workspace, session["session_id"], d["subject_digest"])
    with SqlUnitOfWork(pg) as uow:
        gate = svc.merge_gate(uow, workspace_id=ws, delivery_id=out["delivery_id"])
        assert gate["eligible"], gate
        mr = svc.request_merge(
            uow,
            workspace_id=ws,
            delivery_id=out["delivery_id"],
            expected_head_sha=HEAD_SHA,
            merge_method="squash",
            authorizing_principal=workspace["user_id"],
        )
        uow.commit()
    done = _wait_merge(pg, ws, mr["merge_request_id"])
    assert done["state"] == "succeeded", done
    pr = next(v for v in remote.prs.values())
    assert pr["merged"] is True

    # One active merge per delivery; replay is a conflict, not a repeat effect.
    with SqlUnitOfWork(pg) as uow:
        with pytest.raises(DomainError):
            svc.request_merge(
                uow,
                workspace_id=ws,
                delivery_id=out["delivery_id"],
                expected_head_sha=HEAD_SHA,
                merge_method="squash",
                authorizing_principal=workspace["user_id"],
            )
    handlers.set_delivery_plane(None)


def test_merge_gate_rechecks_checks_and_remote_drift(pg, workspace, tmp_path):
    from control.vcs.remote import FakeRemote

    remote = FakeRemote()
    svc = _make_delivery_service(pg, remote, tmp_path)
    from control.jobs import handlers

    handlers.set_delivery_plane(svc)
    session = _session(pg, workspace)
    cs_id = _fabricate_changeset(pg, workspace, session["session_id"])

    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        out = svc.create(
            uow,
            workspace_id=workspace["workspace_id"],
            session_id=session["session_id"],
            changeset_id=cs_id,
            target={"repository": REPO, "base_ref": "main"},
            transport="pull_request",
            ship_policy={
                "required_result_count": 0,
                "required_check_names": ["ci"],
            },
            authorizing_principal=workspace["user_id"],
        )
        uow.commit()
    d = _wait_delivery(pg, workspace["workspace_id"], out["delivery_id"])
    assert d["state"] == "succeeded"
    ws = workspace["workspace_id"]
    with SqlUnitOfWork(pg) as uow:
        reconcile = uow.rows.one(
            "SELECT result FROM delivery_steps WHERE delivery_id=%s AND kind='reconcile'",
            (out["delivery_id"],),
        )
        assert (reconcile["result"] or {}).get("blocked_checks")
        gate = svc.merge_gate(uow, workspace_id=ws, delivery_id=out["delivery_id"])
        assert not gate["eligible"] and "checks_unsatisfied" in gate["reasons"]
    handlers.set_delivery_plane(None)


def test_direct_base_requires_optin(pg, workspace, tmp_path):
    from control.domain.errors import DomainError
    from control.vcs.remote import FakeRemote

    svc = _make_delivery_service(pg, FakeRemote(), tmp_path)
    session = _session(pg, workspace)
    cs_id = _fabricate_changeset(pg, workspace, session["session_id"])
    from control.persistence.unit_of_work import SqlUnitOfWork

    with SqlUnitOfWork(pg) as uow:
        with pytest.raises(DomainError):
            svc.create(
                uow,
                workspace_id=workspace["workspace_id"],
                session_id=session["session_id"],
                changeset_id=cs_id,
                target={"repository": REPO, "ref": "main"},
                transport="direct_base",
                authorizing_principal=workspace["user_id"],
            )
