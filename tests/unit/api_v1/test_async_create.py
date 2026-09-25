"""SOR-82 A2: async ``POST /v1/agents`` + ``Idempotency-Key`` create contract.

The route returns as soon as the session record + a ``CREATING`` run-1 exist;
a background worker provisions the sandbox, runs ``runner init`` and
dispatches the queued first turn. Duplicates under one key — concurrent or
response-loss retries — resolve to the same agent/run and a single worker.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from control.devin_pool import DevinAccountPool
from tests.unit.api_v1.conftest import create_agent, seed_account, wait_run

_BODY: dict[str, Any] = {
    "prompt": {"text": "Create hello.txt in the workspace."},
    "agent": {"provider": "codex"},
}


def _gate_backend_create(v1_env, monkeypatch) -> tuple[threading.Event, threading.Event, list]:
    """Make ``backend.create`` block until released; returns (entered, release, calls)."""
    entered = threading.Event()
    release = threading.Event()
    calls: list[Any] = []
    orig = v1_env.backend.create

    def gated(spec):
        calls.append(spec)
        entered.set()
        assert release.wait(timeout=30), "test never released backend.create"
        return orig(spec)

    monkeypatch.setattr(v1_env.backend, "create", gated)
    return entered, release, calls


def _gate_runner_init(
    v1_env, monkeypatch, *, fail: bool = False
) -> tuple[threading.Event, threading.Event, list]:
    """Block ``runner init`` inside ``backend.exec`` until released.

    Returns (entered, release, exec_argv_log). With ``fail=True`` the init
    exec raises once released — a provisioning failure landing after a
    cancel. The sandbox is already bound at this point, so this reproduces
    the post-bind / pre-idle window deterministically.
    """
    entered = threading.Event()
    release = threading.Event()
    execs: list[list[str]] = []
    orig = v1_env.backend.exec

    def gated(handle, argv, env=None):
        execs.append(list(argv))
        if "init" in argv:
            entered.set()
            assert release.wait(timeout=30), "test never released runner init"
            if fail:
                raise RuntimeError("runner init exploded")
        return orig(handle, argv, env)

    monkeypatch.setattr(v1_env.backend, "exec", gated)
    return entered, release, execs


def _v1_state(v1_env):
    state = getattr(v1_env.app.state, "v1_state", None)
    assert state is not None
    return state


def _devin_pool(v1_env) -> DevinAccountPool:
    seed_account(
        v1_env,
        "acct-devin-1",
        provider="devin",
        max_concurrent=8,
        models=("swe-2-high",),
        secret_name="sbx-acct-devin-1",
    )
    pool = DevinAccountPool(v1_env.registry, account_id="acct-devin-1")
    v1_env.app.state.scheduler = pool
    return pool


class TestAsyncCreate:
    def test_create_returns_creating_before_provision(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        entered, release, calls = _gate_backend_create(v1_env, monkeypatch)
        started = time.monotonic()
        resp = client.post("/v1/agents", json=dict(_BODY), headers=auth)
        elapsed = time.monotonic() - started
        assert resp.status_code == 201, resp.text
        # Quick return: the response came back while backend.create was blocked.
        assert entered.is_set() or elapsed < 5.0
        body = resp.json()
        agent, run = body["agent"], body["run"]
        assert run["id"] == "run-1"
        assert run["status"] == "CREATING"
        assert agent["status"] == "creating"
        release.set()
        finished = wait_run(client, auth, agent["id"], "run-1")
        assert finished["status"] == "FINISHED"
        assert len(calls) == 1

    def test_run_creating_then_running_then_finished(self, client, auth) -> None:
        body = create_agent(client, auth)
        assert body["run"]["status"] in ("CREATING", "RUNNING", "FINISHED")
        run = wait_run(client, auth, body["agent"]["id"], "run-1")
        assert run["status"] == "FINISHED"
        assert run["result"]["text"]

    def test_startup_failure_persists_error_and_releases_lease(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        pool = _devin_pool(v1_env)

        def boom(spec):
            raise RuntimeError("sandbox cold start exploded")

        monkeypatch.setattr(v1_env.backend, "create", boom)
        resp = client.post(
            "/v1/agents",
            json={"prompt": {"text": "hi"}, "agent": {"provider": "devin"}},
            headers=auth,
        )
        assert resp.status_code == 201, resp.text
        agent_id = resp.json()["agent"]["id"]

        run = wait_run(client, auth, agent_id, "run-1")
        assert run["status"] == "ERROR"
        assert run["error"]["code"] == "runtime_error"
        assert "exploded" in run["error"]["message"]
        # Persisted terminal state survives subsequent reads.
        again = client.get(f"/v1/agents/{agent_id}/runs/run-1", headers=auth).json()
        assert again["status"] == "ERROR"
        # The failed create freed the account slot; session is terminal.
        assert pool.active_count == 0
        assert agent_id not in _v1_state(v1_env).leases
        assert v1_env.store.get(agent_id).status == "lost"

    def test_init_failure_persists_error(self, client, auth, v1_env, monkeypatch) -> None:
        def boom(handle, argv, env=None):
            raise RuntimeError("init exec boom")

        monkeypatch.setattr(v1_env.backend, "exec", boom)
        agent_id = create_agent(client, auth)["agent"]["id"]
        run = wait_run(client, auth, agent_id, "run-1")
        assert run["status"] == "ERROR"
        assert v1_env.store.get(agent_id).status == "lost"

    def test_followup_during_provision_is_409(self, client, auth, v1_env, monkeypatch) -> None:
        entered, release, _ = _gate_backend_create(v1_env, monkeypatch)
        agent_id = create_agent(client, auth)["agent"]["id"]
        assert entered.wait(timeout=5)
        resp = client.post(
            f"/v1/agents/{agent_id}/runs",
            # SOR-224: the default is durable QUEUED — ``reject`` keeps the
            # pre-queue 409 refusal this test asserts.
            json={"prompt": {"text": "too early"}, "on_busy": "reject"},
            headers=auth,
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "turn_in_progress"
        release.set()
        assert wait_run(client, auth, agent_id, "run-1")["status"] == "FINISHED"

    def test_cancel_during_provision_stays_cancelled(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        entered, release, _ = _gate_backend_create(v1_env, monkeypatch)
        agent_id = create_agent(client, auth)["agent"]["id"]
        assert entered.wait(timeout=5)

        resp = client.post(f"/v1/agents/{agent_id}/runs/run-1/cancel", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["status"] == "CANCELLED"

        release.set()
        # The worker must not resurrect the cancelled run once provisioned.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            rec = v1_env.store.get(agent_id)
            if rec is not None and rec.status in ("idle", "running"):
                break
            time.sleep(0.05)
        run = client.get(f"/v1/agents/{agent_id}/runs/run-1", headers=auth).json()
        assert run["status"] == "CANCELLED"
        assert v1_env.store.get(agent_id).status == "idle"
        # No turn ever ran: a follow-up allocates turn-2, never reuses turn-1.
        follow = client.post(
            f"/v1/agents/{agent_id}/runs",
            json={"prompt": {"text": "second"}},
            headers=auth,
        )
        assert follow.status_code == 201
        assert follow.json()["id"] == "run-2"
        assert wait_run(client, auth, agent_id, "run-2")["status"] == "FINISHED"

    def test_cancel_during_init_blocks_followup_until_provisioned(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        """Cancel run-1 while ``runner init`` is still blocked.

        The session must not report ``idle`` early: a follow-up run posted
        during init gets 409 and no ``runner turn`` exec may start inside
        the half-provisioned sandbox (SOR-82 review regression).
        """
        entered, release, execs = _gate_runner_init(v1_env, monkeypatch)
        agent_id = create_agent(client, auth)["agent"]["id"]
        assert entered.wait(timeout=10), "runner init never started"

        cancelled = client.post(f"/v1/agents/{agent_id}/runs/run-1/cancel", headers=auth)
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "CANCELLED"
        # Still provisioning — the record must not claim idle yet.
        assert v1_env.store.get(agent_id).status == "creating"

        early = client.post(
            f"/v1/agents/{agent_id}/runs",
            json={"prompt": {"text": "too early"}},
            headers=auth,
        )
        assert early.status_code == 409, early.text
        assert not any("turn" in argv for argv in execs), (
            "a turn exec started while runner init was still in flight"
        )

        release.set()
        # Once provisioning settles the cancelled-run agent is usable.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            rec = v1_env.store.get(agent_id)
            if rec is not None and rec.status == "idle":
                break
            time.sleep(0.05)
        assert v1_env.store.get(agent_id).status == "idle"
        follow = client.post(
            f"/v1/agents/{agent_id}/runs",
            json={"prompt": {"text": "second"}},
            headers=auth,
        )
        assert follow.status_code == 201, follow.text
        assert follow.json()["id"] == "run-2"
        assert wait_run(client, auth, agent_id, "run-2")["status"] == "FINISHED"
        # run-1 stayed CANCELLED through all of it.
        assert (
            client.get(f"/v1/agents/{agent_id}/runs/run-1", headers=auth).json()["status"]
            == "CANCELLED"
        )

    def test_cancel_during_init_then_init_failure_marks_lost(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        """Cancel run-1, then provisioning fails: the session must go
        ``lost`` — a dead sandbox must not linger as a fake ``idle``."""
        entered, release, _execs = _gate_runner_init(v1_env, monkeypatch, fail=True)
        agent_id = create_agent(client, auth)["agent"]["id"]
        assert entered.wait(timeout=10), "runner init never started"

        cancelled = client.post(f"/v1/agents/{agent_id}/runs/run-1/cancel", headers=auth)
        assert cancelled.status_code == 200, cancelled.text
        release.set()

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            rec = v1_env.store.get(agent_id)
            if rec is not None and rec.status != "creating":
                break
            time.sleep(0.05)
        assert v1_env.store.get(agent_id).status == "lost"
        run = client.get(f"/v1/agents/{agent_id}/runs/run-1", headers=auth).json()
        assert run["status"] == "CANCELLED"
        # A dead agent rejects follow-ups instead of pretending idle.
        follow = client.post(
            f"/v1/agents/{agent_id}/runs",
            json={"prompt": {"text": "second"}},
            headers=auth,
        )
        assert follow.status_code == 409

    def test_delete_during_provision_closes_and_releases_lease(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        pool = _devin_pool(v1_env)
        entered, release, _ = _gate_backend_create(v1_env, monkeypatch)
        resp = client.post(
            "/v1/agents",
            json={"prompt": {"text": "hi"}, "agent": {"provider": "devin"}},
            headers=auth,
        )
        assert resp.status_code == 201, resp.text
        agent_id = resp.json()["agent"]["id"]
        assert entered.wait(timeout=5)

        deleted = client.delete(f"/v1/agents/{agent_id}", headers=auth)
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "closed"
        assert pool.active_count == 0

        release.set()
        run = wait_run(client, auth, agent_id, "run-1")
        assert run["status"] in ("CANCELLED", "ERROR")
        # The provisioned-but-orphaned sandbox is terminated, never bound.
        assert v1_env.store.get(agent_id).status == "closed"


class TestIdempotencyKey:
    def test_retry_same_key_replays_response(self, client, auth, v1_env) -> None:
        headers = {**auth, "Idempotency-Key": "key-retry-1"}
        first = client.post("/v1/agents", json=dict(_BODY), headers=headers)
        assert first.status_code == 201, first.text
        second = client.post("/v1/agents", json=dict(_BODY), headers=headers)
        assert second.status_code == 201
        assert second.json()["agent"]["id"] == first.json()["agent"]["id"]
        assert second.json()["run"]["id"] == first.json()["run"]["id"]
        # One logical agent — no second session record or sandbox.
        assert len(v1_env.store.list_all()) == 1
        wait_run(client, auth, first.json()["agent"]["id"], "run-1")

    def test_concurrent_same_key_creates_one_agent(self, client, auth, v1_env, monkeypatch) -> None:
        entered, release, calls = _gate_backend_create(v1_env, monkeypatch)
        headers = {**auth, "Idempotency-Key": "key-concurrent-1"}
        results: list[Any] = [None, None]

        def post(i: int) -> None:
            results[i] = client.post("/v1/agents", json=dict(_BODY), headers=headers)

        threads = [threading.Thread(target=post, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        assert entered.wait(timeout=5)
        time.sleep(0.1)  # let the duplicate block on the in-flight entry
        release.set()
        for t in threads:
            t.join(timeout=15)

        resps = [r for r in results if r is not None]
        assert len(resps) == 2
        assert all(r.status_code == 201 for r in resps)
        ids = {r.json()["agent"]["id"] for r in resps}
        assert len(ids) == 1
        assert {r.json()["run"]["id"] for r in resps} == {"run-1"}
        assert len(v1_env.store.list_all()) == 1
        assert len(calls) == 1  # exactly one worker was provisioned
        agent_id = resps[0].json()["agent"]["id"]
        assert wait_run(client, auth, agent_id, "run-1")["status"] == "FINISHED"

    def test_same_key_different_body_is_409(self, client, auth) -> None:
        headers = {**auth, "Idempotency-Key": "key-mismatch-1"}
        first = client.post("/v1/agents", json=dict(_BODY), headers=headers)
        assert first.status_code == 201, first.text
        other = {
            "prompt": {"text": "a different prompt"},
            "agent": {"provider": "codex"},
        }
        second = client.post("/v1/agents", json=other, headers=headers)
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "idempotency_conflict"

    def test_different_keys_create_distinct_agents(self, client, auth, v1_env) -> None:
        one = client.post("/v1/agents", json=dict(_BODY), headers={**auth, "Idempotency-Key": "k1"})
        two = client.post("/v1/agents", json=dict(_BODY), headers={**auth, "Idempotency-Key": "k2"})
        assert one.status_code == two.status_code == 201
        assert one.json()["agent"]["id"] != two.json()["agent"]["id"]
        assert len(v1_env.store.list_all()) == 2
        for resp in (one, two):
            wait_run(client, auth, resp.json()["agent"]["id"], "run-1")

    def test_failed_create_does_not_pin_key(self, client, auth, v1_env, monkeypatch) -> None:
        headers = {**auth, "Idempotency-Key": "key-fail-1"}
        v1_env.app.state.plane.max_concurrent = 0
        refused = client.post("/v1/agents", json=dict(_BODY), headers=headers)
        assert refused.status_code == 429
        v1_env.app.state.plane.max_concurrent = 4
        retried = client.post("/v1/agents", json=dict(_BODY), headers=headers)
        assert retried.status_code == 201, retried.text
        wait_run(client, auth, retried.json()["agent"]["id"], "run-1")


class TestWorkerCleanup:
    def test_provision_failure_under_pool_releases_lease(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        """SOR-80 lease cleanup, async path: startup failure frees the slot."""
        pool = _devin_pool(v1_env)

        def boom(spec):
            raise RuntimeError("create exploded")

        monkeypatch.setattr(v1_env.backend, "create", boom)
        resp = client.post(
            "/v1/agents",
            json={"prompt": {"text": "hi"}, "agent": {"provider": "devin"}},
            headers=auth,
        )
        assert resp.status_code == 201, resp.text
        agent_id = resp.json()["agent"]["id"]
        wait_run(client, auth, agent_id, "run-1")
        assert pool.active_count == 0
        assert agent_id not in _v1_state(v1_env).leases
