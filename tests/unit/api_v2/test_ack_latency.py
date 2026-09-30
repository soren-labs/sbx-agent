"""SOR-265 regression coverage — mutation routes ACK on a bounded wait.

``POST /v2/sessions``, ``POST /v2/sessions/{id}/messages`` and
``POST /v2/sessions/{id}/cancel`` must never serialize on Modal/sandbox
work: provisioning, eligibility eval, run dispatch and sandbox exec all
run on a worker thread, and the request answers within
``app.state.v2_ack_budget_s`` (contract-legal optimistic views when the
worker is still in flight). These tests pin:

- no request-path ``backend.exec``/``backend.poll`` on any of the three
  routes — proven by gating the backend on an unset event (if the call
  were on-path the request would stall for the full gate timeout);
- bounded store ops on the request path (a counting proxy, bounded while
  the worker is deterministically stalled);
- optimistic ACK semantics — ``queued``/``provisioning``/``cancelled``
  surface while the async work lands, and the session still converges.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient
from tests.unit.api_v1.conftest import V1Env
from tests.unit.api_v2.conftest import create_session, wait_session


class _Counting:
    """Proxy a store, counting method calls (request-path op bound)."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.calls = 0

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def _wrapped(*args, **kwargs):
            self.calls += 1
            return attr(*args, **kwargs)

        return _wrapped


@pytest.fixture
def zero_budget(v1_env: V1Env) -> V1Env:
    """Force the optimistic branch — the bound the routes must keep."""
    v1_env.app.state.v2_ack_budget_s = 0.0
    return v1_env


def _gate_backend(v1_env: V1Env, monkeypatch: pytest.MonkeyPatch, *names: str) -> threading.Event:
    """Wrap backend methods so they block until the returned event is set.

    If any gated call still runs on the request path, the request stalls
    for the whole gate timeout — observable as a >1s response.
    """
    gate = threading.Event()
    backend = v1_env.backend
    for name in names:
        original = getattr(backend, name)

        def _gated(*args, __original=original, **kwargs):
            gate.wait(60)
            return __original(*args, **kwargs)

        monkeypatch.setattr(backend, name, _gated)
    return gate


def test_create_acks_without_request_path_work(
    client: TestClient,
    v1_env: V1Env,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create holds a bounded wait on provisioning — not the reverse."""
    counting = _Counting(v1_env.app.state.task_store)
    v1_env.app.state.task_store = counting

    # Stall the dispatch worker before it can bind an agent: a blocked
    # open_session means the request path itself must still return promptly
    # and must not issue sandbox calls of its own.
    open_gate = threading.Event()
    original_open = v1_env.app.state.plane.open_session

    def _open(*args, **kwargs):
        open_gate.wait(60)
        return original_open(*args, **kwargs)

    monkeypatch.setattr(v1_env.app.state.plane, "open_session", _open)
    backend_gate = _gate_backend(v1_env, monkeypatch, "exec", "poll", "create")

    started = time.monotonic()
    resp = client.post(
        "/v2/sessions",
        json={"prompt": "hello", "execution": {"provider": "codex"}},
        headers={**auth, "Idempotency-Key": "ack-create-1"},
    )
    elapsed = time.monotonic() - started

    assert resp.status_code == 201, resp.text
    session = resp.json()["session"]
    assert session["id"].startswith("sess_")
    assert session["status"] == "queued"
    assert session["phase"] == "provisioning"
    assert elapsed < 1.0, elapsed
    # Request path = record put + post-wait get + the keyed replay lookup.
    assert counting.calls <= 4, counting.calls

    open_gate.set()
    backend_gate.set()
    settled = wait_session(client, auth, session["id"], "running", "finished")
    assert settled["session"]["execution"]["provider"] == "codex"


def test_create_dispatch_failure_keeps_error_contract(
    client: TestClient,
    auth: dict[str, str],
) -> None:
    """A fast-failing dispatch still surfaces its canonical error code."""
    resp = client.post(
        "/v2/sessions",
        json={"prompt": "hello", "execution": {"provider": "grok"}},
        headers=auth,
    )
    assert resp.status_code == 429, resp.text
    assert resp.json()["error"]["code"] == "provider_exhausted"


def test_message_acks_without_sandbox_exec(
    client: TestClient,
    v1_env: V1Env,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Follow-up dispatch (write_file + exec spawn) runs off the request."""
    session = create_session(client, auth)["session"]
    wait_session(client, auth, session["id"], "finished")

    # FINISHED publishes the run verdict before the eager checkpoint ends.
    # This test gates dispatch, so wait for the agent to become runnable;
    # otherwise a valid queued ACK can complete without touching exec.
    task = v1_env.app.state.task_store.get(session["id"])
    until = time.monotonic() + 5
    while time.monotonic() < until:
        rec = v1_env.app.state.plane.get(task.agent_id)
        if rec.status == "idle" and rec.current_turn_id is None:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("agent did not finish checkpoint settlement")

    counting = _Counting(v1_env.app.state.task_store)
    v1_env.app.state.task_store = counting
    gate = _gate_backend(v1_env, monkeypatch, "exec", "poll")
    started = time.monotonic()
    resp = client.post(
        f"/v2/sessions/{session['id']}/messages",
        json={"prompt": "second turn"},
        headers=auth,
    )
    elapsed = time.monotonic() - started

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert elapsed < 1.0, elapsed
    # session get on the request path + the worker's own lookup — bounded
    # while the dispatch is stalled at the gated exec.
    assert counting.calls <= 4, counting.calls
    # Worker still in flight at ACK → the nullable message slot carries
    # the optimistic accept.
    assert body["message"] is None
    assert body["session"]["status"] in ("queued", "running")

    gate.set()
    wait_session(client, auth, session["id"], "finished")
    detail = client.get(f"/v2/sessions/{session['id']}", headers=auth).json()
    assert any(r["n"] == 2 for r in detail["runs"])


def test_cancel_acks_without_sandbox_exec(
    client: TestClient,
    v1_env: V1Env,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel's stop-hook exec no longer blocks the ACK."""
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
    session = create_session(client, auth)["session"]
    wait_session(client, auth, session["id"], "running")

    gate = _gate_backend(v1_env, monkeypatch, "exec", "poll")
    started = time.monotonic()
    resp = client.post(f"/v2/sessions/{session['id']}/cancel", headers=auth)
    elapsed = time.monotonic() - started

    assert resp.status_code == 200, resp.text
    assert elapsed < 1.0, elapsed
    assert resp.json()["session"]["status"] == "cancelled"

    gate.set()
    wait_session(client, auth, session["id"], "cancelled")


def _gate_open_session(v1_env: V1Env, monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Hold the create worker pre-bind (its first persistence call)."""
    gate = threading.Event()
    original = v1_env.app.state.plane.open_session

    def _open(*args, **kwargs):
        gate.wait(60)
        return original(*args, **kwargs)

    monkeypatch.setattr(v1_env.app.state.plane, "open_session", _open)
    return gate


def test_message_during_provisioning_is_refused(
    client: TestClient,
    v1_env: V1Env,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_gate = _gate_open_session(v1_env, monkeypatch)
    session = create_session(client, auth)["session"]
    assert session["status"] == "queued"
    resp = client.post(
        f"/v2/sessions/{session['id']}/messages",
        json={"prompt": "too early"},
        headers=auth,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "session_not_runnable"
    open_gate.set()


def test_cancel_during_provisioning_still_cancels(
    client: TestClient,
    v1_env: V1Env,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel racing the deferred bind wins — the agent cancels on land."""
    open_gate = _gate_open_session(v1_env, monkeypatch)
    session = create_session(client, auth)["session"]
    resp = client.post(f"/v2/sessions/{session['id']}/cancel", headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["session"]["status"] == "cancelled"
    # The dispatch lands after the cancel ACK — its post-bind check must
    # still cancel the just-created agent.
    open_gate.set()
    wait_session(client, auth, session["id"], "cancelled")


def test_session_converges_after_optimistic_ack(
    client: TestClient,
    zero_budget: V1Env,
    auth: dict[str, str],
) -> None:
    """The optimistic ACK is durable — the session finishes normally."""
    session = create_session(client, auth)["session"]
    assert session["phase"] == "provisioning"
    settled = wait_session(client, auth, session["id"], "running", "finished")
    assert settled["session"]["id"] == session["id"]
