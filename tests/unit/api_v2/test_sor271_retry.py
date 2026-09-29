"""SOR-271: retryable pre-bind dispatch failures + list terminal convergence.

A session that fails before its agent ever binds (concurrency_limit,
account_unavailable, provider_exhausted, workspace resolution) has
``record.agent_id is None`` — there is no bound agent to re-post a run to,
so the V1 ``retry_task`` path answered ``session_not_runnable`` forever.
The repair re-drives the original dispatch on the same session id.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from tests.unit.api_v1.conftest import V1Env
from tests.unit.api_v2.conftest import wait_session

BODY = {"prompt": "Create hello.txt in the workspace.", "execution": {"provider": "codex"}}


def _create_prebind_failure(
    client: TestClient,
    auth: dict[str, str],
    env: V1Env,
) -> dict:
    """Create a session whose dispatch fails pre-bind on the concurrency cap.

    The dispatch completes inside the ack budget, so create answers 429 —
    but the provisional session record was already persisted and settles
    to failed. Either status code leaves a failed agent-less session.
    """
    env.app.state.plane.max_concurrent = 0
    resp = client.post("/v2/sessions", json=BODY, headers=auth)
    assert resp.status_code in (201, 429), resp.text
    listing = client.get("/v2/sessions", headers=auth).json()
    assert len(listing["sessions"]) == 1
    session = wait_session(client, auth, listing["sessions"][0]["id"], "failed")
    return session["session"]


class TestPrebindRetry:
    def test_retryable_prebind_failure_redispatches(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env
    ) -> None:
        session = _create_prebind_failure(client, auth, credentialed)
        assert session["error"]["code"] == "concurrency_limit"
        assert session["error"]["retryable"] is True
        session_id = session["id"]

        # Retry while still capped: the dispatch re-runs and fails again —
        # it must NOT answer the permanent ``session_not_runnable``.
        resp = client.post(f"/v2/sessions/{session_id}/retry", json={}, headers=auth)
        assert resp.status_code == 429, resp.text
        assert resp.json()["error"]["code"] == "concurrency_limit"

        failed_again = wait_session(client, auth, session_id, "failed")
        assert failed_again["session"]["error"]["code"] == "concurrency_limit"

        # Capacity restored: the same retry binds an agent and runs.
        credentialed.app.state.plane.max_concurrent = 64
        resp = client.post(f"/v2/sessions/{session_id}/retry", json={}, headers=auth)
        assert resp.status_code == 200, resp.text
        assert resp.json()["run"]["n"] == 1
        done = wait_session(client, auth, session_id, "finished")
        assert done["session"]["error"] is None

        # Attempt history is preserved across the re-drive.
        record = credentialed.app.state.task_store.get(session_id)
        reasons = [t["reason"] for t in record.transitions]
        assert reasons.count("dispatch_failed") == 2
        assert reasons.count("retry_dispatch") == 2
        assert record.agent_id is not None

        # The list agrees with the detail view.
        listing = client.get("/v2/sessions", headers=auth).json()
        assert listing["sessions"][0]["status"] == "finished"

    def test_non_retryable_prebind_failure_rejected(
        self, client: TestClient, auth: dict[str, str], git_repo
    ) -> None:
        url, _ = git_repo
        resp = client.post(
            "/v2/sessions",
            json={**BODY, "repository": {"repo": url, "ref": "not a safe ref!!"}},
            headers=auth,
        )
        assert resp.status_code in (201, 400), resp.text
        listing = client.get("/v2/sessions", headers=auth).json()
        session = wait_session(client, auth, listing["sessions"][0]["id"], "failed")
        assert session["session"]["error"]["code"] == "workspace_invalid"
        assert session["session"]["error"]["retryable"] is False

        retry = client.post(f"/v2/sessions/{session['session']['id']}/retry", json={}, headers=auth)
        assert retry.status_code == 409, retry.text
        assert retry.json()["error"]["code"] == "task_not_retryable"

    def test_delivery_mode_retry_rejected_on_dispatch_failure(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env
    ) -> None:
        session = _create_prebind_failure(client, auth, credentialed)
        resp = client.post(
            f"/v2/sessions/{session['id']}/retry",
            json={"mode": "delivery"},
            headers=auth,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "task_not_retryable"

    def test_retry_prompt_override_reaches_redispatch(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env
    ) -> None:
        session = _create_prebind_failure(client, auth, credentialed)
        credentialed.app.state.plane.max_concurrent = 64
        resp = client.post(
            f"/v2/sessions/{session['id']}/retry",
            json={"prompt": "Create goodbye.txt instead."},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["run"]["prompt"] == "Create goodbye.txt instead."
        record = credentialed.app.state.task_store.get(session["id"])
        assert record.request["prompt"]["text"] == "Create goodbye.txt instead."

    def test_cancelled_agent_less_record_not_retryable(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env
    ) -> None:
        session = _create_prebind_failure(client, auth, credentialed)
        store = credentialed.app.state.task_store
        record = store.get(session["id"])
        # The state a pre-bind cancel leaves: terminal, no agent ever bound.
        record.status = "cancelled"
        record.transitions.append({"status": "cancelled", "reason": "cancelled", "at": "t"})
        store.put(record)
        resp = client.post(f"/v2/sessions/{session['id']}/retry", json={}, headers=auth)
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "task_not_retryable"
