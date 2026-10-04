"""FINAL-V2-001: an accepted reviewer survives loss of its startup executor."""

import sys
from concurrent.futures import Future

from control.api_v2 import routes
from control.app import create_app
from control.auth_store import AuthDatabase, AuthStore, PersistentApiKeyStore
from fastapi.testclient import TestClient
from tests.unit.api_v2.conftest import create_session, wait_session
from tests.unit.test_hosted_workflow import workflow_app as workflow_app


def reconstruct(app):
    return create_app(
        backend=app.state.compute_provider.source,
        auth_store=AuthStore(AuthDatabase(path=app.state.auth_store.database._path)),
        hosted=True,
        state_backend="postgres",
        connection_vault=app.state.connections.vault,
        runner_cmd=[sys.executable, "-m", "runtime.runner"],
    )


def test_accepted_reviewer_restart_is_retryable_and_old_worker_is_fenced(workflow_app, monkeypatch):
    app, user, headers, repo = workflow_app
    pending = []
    with TestClient(app, base_url="https://testserver") as client:
        author = create_session(client, headers, repository={"repo": repo, "ref": "main"})[
            "session"
        ]["id"]
        wait_session(client, headers, author, "finished")
        assert (
            client.post(f"/v2/sessions/{author}/deliver", headers=headers, json={}).status_code
            == 200
        )
        with monkeypatch.context() as patch:
            patch.setattr(routes._HEAVY_POOL, "submit", lambda fn: pending.append(fn) or Future())
            ack = client.post(
                f"/hosted/sessions/{author}/review-sessions", headers=headers, json={}
            )
        assert ack.status_code == 201
        reviewer = ack.json()["session_id"]
        assert app.state.task_store.get(reviewer).agent_id is None
    restored = reconstruct(app)
    with TestClient(restored, base_url="https://testserver") as client:
        result = client.get(f"/hosted/review-sessions/{reviewer}", headers=headers)
        assert result.json()["status"] == "failed"
        assert result.json()["retryable"] is True
        other = restored.state.auth_store.create_user(email="startup-other@example.test")
        token = PersistentApiKeyStore(restored.state.auth_store).create(user_id=other.id)[1]
        foreign = {"Authorization": f"Bearer {token}"}
        assert (
            client.post(f"/v2/sessions/{reviewer}/retry", headers=foreign, json={}).status_code
            == 404
        )
        assert restored.state.task_store.get(reviewer).owner == user.id
        retry = client.post(f"/v2/sessions/{reviewer}/retry", headers=headers, json={})
        assert retry.status_code in (200, 202)
        wait_session(client, headers, reviewer, "finished")
        bound = restored.state.task_store.get(reviewer).agent_id
        assert bound
        for fn in pending:
            fn()
        assert restored.state.task_store.get(reviewer).agent_id == bound
        assert (
            client.get(f"/hosted/review-sessions/{reviewer}", headers=headers).json()["status"]
            == "completed"
        )


def accept_paused(client, headers, monkeypatch, **overrides):
    pending = []
    with monkeypatch.context() as patch:
        patch.setattr(routes._HEAVY_POOL, "submit", lambda fn: pending.append(fn) or Future())
        response = create_session(client, headers, **overrides)
    return response["session"]["id"], pending


def test_restart_create_replay_keeps_intent_id_and_rejects_changed_body(workflow_app, monkeypatch):
    app, user, headers, repo = workflow_app
    headers = {**headers, "Idempotency-Key": "startup-replay"}
    with TestClient(app, base_url="https://testserver") as client:
        sid, pending = accept_paused(client, headers, monkeypatch)
        original = app.state.task_store.get(sid)
    restored = reconstruct(app)
    with TestClient(restored, base_url="https://testserver") as client:
        replay = create_session(client, headers)
        assert replay["session"]["id"] == sid
        failed = restored.state.task_store.get(sid)
        assert failed.status == "error" and failed.request == original.request
        assert failed.idempotency == original.idempotency
        assert (
            client.post("/v2/sessions", headers=headers, json={"prompt": "different"}).status_code
            == 409
        )
        pending[0]()
        assert restored.state.task_store.get(sid).agent_id is None
        assert len(restored.state.task_store.list(owner=user.id)) == 1


def test_reconciliation_preserves_live_current_claim_and_terminal_cancel(workflow_app, monkeypatch):
    app, user, headers, repo = workflow_app
    with TestClient(app, base_url="https://testserver") as client:
        sid, pending = accept_paused(client, headers, monkeypatch)
        assert app.state.startup_dispatch.reconcile() == 0
        assert app.state.task_store.get(sid).status == "queued"
        assert client.post(f"/v2/sessions/{sid}/cancel", headers=headers, json={}).status_code in (
            200,
            202,
        )
        assert app.state.task_store.get(sid).status == "cancelled"
    restored = reconstruct(app)
    with TestClient(restored, base_url="https://testserver"):
        pending[0]()
        record = restored.state.task_store.get(sid)
        assert record.status == "cancelled" and record.agent_id is None


def test_old_binding_cannot_overwrite_new_attempt_and_closes_only_its_agent(
    workflow_app, monkeypatch
):
    from copy import deepcopy
    from types import SimpleNamespace

    import pytest
    from control.api_v1.errors import V1ApiError
    from control.startup_dispatch import StartupDispatch

    app, user, headers, repo = workflow_app
    with TestClient(app, base_url="https://testserver") as client:
        sid, pending = accept_paused(client, headers, monkeypatch)
        original = app.state.task_store.get(sid)
        closed = []
        released = []
        agents = {"old-agent": SimpleNamespace(owner=user.id)}
        plane = SimpleNamespace(store=SimpleNamespace(get=agents.get), close=closed.append)
        v1 = SimpleNamespace(reconcile_leases=lambda *a, **kw: released.append(True))
        old_writer = app.state.startup_dispatch.guarded(original, plane, v1)
        new = StartupDispatch(app.state.task_store)
        assert new.reconcile() == 1
        failed = app.state.task_store.get(sid)
        retry = deepcopy(failed)
        retry.status = "queued"
        retry.transitions.append({"reason": "retry_dispatch", "status": "queued"})
        new.retry(failed, retry)
        # A second retry from the same failed snapshot loses its durable claim.
        with pytest.raises(V1ApiError):
            new.retry(failed, deepcopy(retry))
        winner = deepcopy(retry)
        winner.agent_id = "new-agent"
        new.guarded(retry, plane, v1).put(winner)
        late = deepcopy(original)
        late.agent_id = "old-agent"
        with pytest.raises(V1ApiError):
            old_writer.put(late)
        assert closed == ["old-agent"] and released == [True]
        assert app.state.task_store.get(sid).agent_id == "new-agent"
        assert new.reconcile() == 0
        # A delayed failure must not poison the winning retry either.
        routes._mark_dispatch_failed(old_writer, sid, RuntimeError("old worker failed"))
        assert app.state.task_store.get(sid).agent_id == "new-agent"


def test_legacy_unclaimed_accepted_task_settles_but_other_tasks_do_not(workflow_app, monkeypatch):
    from copy import deepcopy

    from control.startup_dispatch import StartupDispatch

    app, user, headers, repo = workflow_app
    with TestClient(app, base_url="https://testserver") as client:
        sid, _ = accept_paused(client, headers, monkeypatch)
        legacy = app.state.task_store.get(sid)
        legacy.transitions[-1].pop("detail")
        app.state.task_store.put(legacy)
        unrelated = deepcopy(legacy)
        unrelated.id = "task_legacy"
        app.state.task_store.put(unrelated)
        reconciler = StartupDispatch(app.state.task_store)
        assert reconciler.reconcile() == 1
        assert reconciler.reconcile() == 0
        assert app.state.task_store.get(sid).status == "error"
        assert app.state.task_store.get(unrelated.id).status == "queued"


def test_reaper_startup_scan_cannot_settle_a_binding_that_won_the_race(workflow_app, monkeypatch):
    from copy import deepcopy

    from control.startup_dispatch import StartupDispatch

    app, user, headers, repo = workflow_app
    with TestClient(app, base_url="https://testserver") as client:
        sid, _ = accept_paused(client, headers, monkeypatch)
        compare = app.state.task_store.compare_put

        def bind_before_compare(expected, failed):
            bound = deepcopy(expected)
            bound.agent_id = "already-bound"
            app.state.task_store.put(bound)
            return compare(expected, failed)

        with monkeypatch.context() as patch:
            patch.setattr(app.state.task_store, "compare_put", bind_before_compare)
            assert StartupDispatch(app.state.task_store).reconcile() == 0
        assert app.state.task_store.get(sid).agent_id == "already-bound"
        assert app.state.task_store.get(sid).status == "queued"
